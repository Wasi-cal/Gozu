# Copyright (c) 2026 Calfus Inc.
# Author: Wasiullah Rafeeq S

"""
Last-resort handler for a genuinely unexpected exception escaping the
whole `gozu` invocation - only reached for exceptions that aren't Typer/
Click's own control-flow exceptions (typer.Exit, SystemExit,
KeyboardInterrupt), which already exited cleanly on their own.

Deliberately does NOT send email automatically - that would need gozu to
hold its own SMTP/API credentials, could silently fail exactly when
mail infra is what's broken, and risks an unreviewed traceback landing
in an inbox. Instead a pre-filled mailto: link is shown, and the raw
traceback only ever touches a timestamped file under LOGS_DIR.
"""

import traceback
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import typer
from rich.console import Console

from core.constants import CONTACT_EMAILS
from scripts.paths import LOGS_DIR


def _write_traceback_log(exc: BaseException) -> Path:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    log_path = LOGS_DIR / f"crash-{timestamp}.log"
    log_path.write_text("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
    return log_path


def _build_mailto_link(log_path: Path) -> str:
    """mailto: links can't attach files or reliably fit a full traceback,
    so the body just references the log file's path."""
    recipients = ",".join(CONTACT_EMAILS)
    subject = quote("Gozu error report")
    body = quote(
        "gozu hit an unexpected error.\n\n"
        f"Full details were logged to: {log_path}\n\n"
        "Please attach that file, or paste its contents below, before sending."
    )
    return f"mailto:{recipients}?subject={subject}&body={body}"


def handle_unexpected_exception(exc: BaseException) -> None:
    log_path = _write_traceback_log(exc)
    mailto_link = _build_mailto_link(log_path)

    typer.echo()
    typer.echo("Something went wrong that gozu didn't expect.")
    typer.echo(f"Full details were logged to: {log_path}")
    typer.echo()
    typer.echo("If you'd like to report this:")
    # rich's link markup only emits the OSC 8 escape sequence for a real
    # terminal; piped/redirected output gets plain visible text instead,
    # never raw escape bytes dumped into a file.
    Console().print(f"  [link={mailto_link}]Report this error[/link]")
