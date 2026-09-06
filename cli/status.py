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
"""

import typer

SUCCESS = "✅"  # (check mark button)
ERROR = "❌"  # (cross mark)
WARNING = "⚠️"  # (warning sign)
WAITING = "⏳"  # (hourglass)


def success(message: str) -> None:
    typer.secho(f"{SUCCESS} {message}", fg=typer.colors.GREEN)


def error(message: str) -> None:
    typer.secho(f"{ERROR} {message}", fg=typer.colors.RED)


def warning(message: str) -> None:
    typer.secho(f"{WARNING} {message}", fg=typer.colors.YELLOW)


def waiting(message: str) -> None:
    typer.secho(f"{WAITING} {message}", dim=True)
