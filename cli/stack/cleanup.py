# Copyright (c) 2026 Calfus Inc.
# Author: Wasiullah Rafeeq S
#
# Depends on: cli/stack/profiles.py - stopping whatever services this invocation started

"""
Scoped SIGINT/SIGTERM/KeyboardInterrupt cleanup for commands that can
bring Docker services up during their own run (`gozu init`, `gozu up` -
confirmed by reading their actual code; `gozu run` never touches Docker
itself, only host-side Java/sonar-scanner downloads, so it has nothing to
register here).

Deliberately NOT a global "tear everything down" handler - only whatever
THIS invocation itself started fresh (see cli/stack/profiles.py's
ensure_service_up()/is_service_up(), which is how a caller knows the
difference) gets stopped on an abrupt interrupt. A service this invocation
found already running and healthy - from an earlier `gozu up`, or a
completely unrelated docker-compose stack a user happens to have up - is
left exactly as it was; this is scoped per-invocation, not "stop
everything active right now".
"""

import signal
import subprocess
import sys
from pathlib import Path
from types import FrameType

from cli.stack.profiles import stop
from cli.status import success, warning


class InterruptCleanup:
    """
    Usage::

        with InterruptCleanup(stack_dir) as cleanup:
            if ensure_postgres_up(stack_dir):
                cleanup.track("postgres")
            cleanup.run(["docker", "compose", "up", "-d"], cwd=stack_dir)

    Registers SIGINT/SIGTERM handlers on entry (restoring whatever was
    there before on exit, so this never leaks a process-global handler
    past the `with` block) and also catches a plain KeyboardInterrupt
    around the block as a fallback - belt-and-suspenders, since which of
    the two actually fires for a given Ctrl-C can depend on exactly what
    Python is blocked on at that instant (a subprocess call vs a plain
    Python-level wait).

    Use `run()` instead of `subprocess.run()` directly for any
    docker-compose invocation that can block for a while (bringing a
    service up, waiting on a healthcheck) - confirmed live that
    interrupting the *Python* side alone (abandoning subprocess.run()'s
    wait) does NOT, by itself, stop the underlying docker-compose child
    process; it can keep running orphaned in the background and race the
    cleanup's own explicit `stop()` call. A real terminal's Ctrl-C sends
    SIGINT to the whole foreground process group at once, so in normal
    interactive use the child receives it directly too and aborts on its
    own (confirmed separately: docker compose itself exits cleanly on a
    direct SIGINT) - `run()` removes the dependency on that happening by
    proactively terminating the tracked child itself, regardless of
    whether the shell/terminal in a given environment actually delivers
    the signal to it directly.
    """

    def __init__(self, stack_dir: Path):
        self.stack_dir = stack_dir
        self._started: list[str] = []
        self._cleaned_up = False
        self._active_process: subprocess.Popen | None = None
        self._previous_sigint: signal.Handlers | None = None
        self._previous_sigterm: signal.Handlers | None = None

    def track(self, service: str) -> None:
        """Register that this invocation itself started `service` fresh - call this only when ensure_service_up()/an equivalent actually returned True."""
        self._started.append(service)

    def run(self, command: list[str], cwd: Path) -> subprocess.CompletedProcess:
        """
        A `subprocess.run()` stand-in that also registers the child so an
        interrupt firing while it's still running can terminate it
        directly, not just abandon Python's own wait on it.
        """
        process = subprocess.Popen(command, cwd=cwd)
        self._active_process = process
        try:
            returncode = process.wait()
        finally:
            self._active_process = None
        return subprocess.CompletedProcess(command, returncode)

    def _cleanup(self) -> None:
        if self._cleaned_up:
            return
        self._cleaned_up = True

        if self._active_process is not None and self._active_process.poll() is None:
            self._active_process.terminate()

        if not self._started:
            warning("Interrupted - nothing this invocation started needs cleaning up.")
            return

        warning(f"Interrupted - stopping what this invocation started fresh: {', '.join(self._started)}")
        stop(profiles=set(), stack_dir=self.stack_dir, services=self._started)
        success(f"Cleaned up: {', '.join(self._started)}")

    def _handle_signal(self, signum: int, frame: FrameType | None) -> None:
        self._cleanup()
        sys.exit(130 if signum == signal.SIGINT else 143)

    def __enter__(self) -> "InterruptCleanup":
        self._previous_sigint = signal.signal(signal.SIGINT, self._handle_signal)
        self._previous_sigterm = signal.signal(signal.SIGTERM, self._handle_signal)
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc_val: BaseException | None, exc_tb) -> bool:
        signal.signal(signal.SIGINT, self._previous_sigint)
        signal.signal(signal.SIGTERM, self._previous_sigterm)
        if exc_type is KeyboardInterrupt:
            self._cleanup()
            return True  # suppress - already handled, don't propagate a traceback
        return False
