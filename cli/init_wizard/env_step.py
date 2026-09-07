# Copyright (c) 2026 Calfus Inc.
# Author: Wasiullah Rafeeq S
# Editor: Claude
#
# Depends on: cli/prerequisites/__init__.py - ensuring Java/sonar-scanner are installed
# Depends on: scripts/bootstrap_env.py - writing/loading .env fields and resolving ports

"""Step 1-2 of the init wizard: .env bootstrap and Java prerequisite check."""

import questionary
import typer

from cli.prerequisites import ensure_java, ensure_sonar_scanner
from cli.prompts import ask_or_exit
from cli.status import error, waiting
from scripts.bootstrap_env import (
    DEFAULT_PORTS,
    ENV_PATH,
    FernetKeySafetyError,
    bootstrap_env,
    load_into_environ,
    parse_env_file,
    resolve_ports,
)


def step_bootstrap_env() -> None:
    typer.secho("Step 1/3: environment (.env)", bold=True)

    # Prefer whatever's already in .env for a port that's already set - a
    # fresh probe would see it as "taken" once the service it belongs to
    # (e.g. SonarQube itself) is actually running on it, and wrongly
    # suggest the next free port instead of the correct, already-working one.
    existing = parse_env_file(ENV_PATH)
    probed = resolve_ports()
    ports = {name: int(existing[name]) if name in existing else probed[name] for name in DEFAULT_PORTS}

    typer.echo("Ports that will be used (auto-detected as free - override any of them below):")
    overrides: dict[str, int] = {}
    for name, port in ports.items():
        answer = ask_or_exit(questionary.text(f"  {name}", default=str(port)))
        overrides[name] = int(answer)

    try:
        new_fields = bootstrap_env(port_overrides=overrides)
    except FernetKeySafetyError as e:
        error(f"Refusing to continue: {e}")
        raise typer.Exit(code=1) from e

    if new_fields:
        typer.echo("Wrote new .env field(s): " + ", ".join(new_fields))
    else:
        typer.echo(".env already had everything needed - left untouched.")

    # Picks up whatever bootstrap_env() just wrote (a first-ever run has
    # nothing in os.environ yet - the app-level callback only loaded
    # .env's *previous* contents, before this function possibly added more).
    load_into_environ()


def step_ensure_prerequisites() -> None:
    """
    Both Local and Cloud configs run sonar-scanner on THIS host - Cloud
    only means the scan target is SonarQube Cloud instead of a local
    instance, the CLI tool doing the scanning is the same either way (see
    cli/scan_runner/scanner_exec.py's _build_scanner_command(), which
    calls ensure_sonar_scanner() unconditionally for both scanner_modes).
    Resolving both here means neither is ever a surprise download during
    someone's first `gozu run` - run_scan_cycle()'s own calls to these
    stay in place too, as a defensive, idempotent fallback.
    """
    waiting("Checking prerequisites (Java, sonar-scanner) ...")
    ensure_java()
    ensure_sonar_scanner()
