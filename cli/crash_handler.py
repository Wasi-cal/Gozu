# Copyright (c) 2026 Calfus Inc.
# Author: Wasiullah Rafeeq S

"""
Last-resort handler for a genuinely unexpected exception escaping the whole
`gozu` invocation - see cli/main.py's `main()`, the sole caller, which wraps
`app()` and only reaches this for an exception that ISN'T one of Typer/
Click's own deliberate control-flow exceptions (typer.Exit, SystemExit,
KeyboardInterrupt) - those already exited cleanly on their own and never
reach here.

Deliberately does NOT send email automatically: that would require gozu to
hold its own SMTP/API credentials (a new class of secret this codebase has
otherwise been careful to avoid), could silently fail exactly when
network/mail infra is what's broken, and risks a raw traceback landing in
an inbox with nobody having reviewed it first. Instead, a pre-filled
mailto: link is built and shown - a person still decides whether/what to
send - and the raw traceback never touches the terminal at all, only a
timestamped file under LOGS_DIR.
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
    """
    mailto: links can't attach files and have no reliable length budget for
    a full traceback in the body - so the body only references the log
    file's path and asks the sender to attach/paste it themselves, rather
    than trying to embed the traceback text directly.
    """
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
    # rich's own link markup, not a raw escape sequence built by hand -
    # Console.print() only emits the actual OSC 8 hyperlink escape
    # sequence when it detects a real terminal (Console.is_terminal);
    # piped/redirected output (a log capture, a non-interactive CI
    # runner) gets the plain visible text with no escape codes at all,
    # confirmed live - never raw escape bytes dumped into a file. A real
    # terminal that IS attached but doesn't understand OSC 8 still gets
    # a well-formed escape sequence it's expected to silently pass
    # through per the OSC 8 spec, leaving just the visible text - the
    # same plain-text floor as before this change, not something new to
    # implement here. The mailto: URL's own content (recipients,
    # subject, URL-encoded body referencing the log path) is completely
    # unchanged - only the visible label changes from the raw URL to
    # "Report this error".
    Console().print(f"  [link={mailto_link}]Report this error[/link]")
