# Copyright (c) 2026 Calfus Inc.
# Author: Wasiullah Rafeeq S

"""
Shared status symbols for CLI output - one meaning per symbol, used
consistently across the init wizard, gozu up/down/run, and prerequisite
downloads, instead of ad hoc plain-text "Done"/"Failed"/"Waiting...":

  SUCCESS (v)  - something completed / is healthy / is ready
  ERROR   (x)  - something failed / is unhealthy / is down
  WARNING (!)  - a destructive action, a caveat, a degraded-but-running state
  WAITING (...)- in progress: polling, downloading, waiting on a healthcheck

Plain, portable Unicode symbols, not pictograph-style emoji - deliberately
built via named unicode escapes (rather than pasting the literal glyph)
so the exact code point is unambiguous in source, and specifically
WITHOUT a trailing U+FE0F variation selector - that's the specific
character that turns a base glyph like U+26A0 into a colorful emoji
rendering on terminals/fonts that support the distinction, instead of
the plain monochrome symbol used here.
"""

import typer

SUCCESS = "\N{CHECK MARK}"  # ✓ U+2713
ERROR = "\N{BALLOT X}"  # ✗ U+2717
WARNING = "\N{WARNING SIGN}"  # ⚠ U+26A0 - no U+FE0F variation selector
WAITING = "\N{HORIZONTAL ELLIPSIS}"  # … U+2026


def success(message: str) -> None:
    typer.secho(f"{SUCCESS} {message}", fg=typer.colors.GREEN)


def error(message: str) -> None:
    typer.secho(f"{ERROR} {message}", fg=typer.colors.RED)


def warning(message: str) -> None:
    typer.secho(f"{WARNING} {message}", fg=typer.colors.YELLOW)


def waiting(message: str) -> None:
    typer.secho(f"{WAITING} {message}", dim=True)
