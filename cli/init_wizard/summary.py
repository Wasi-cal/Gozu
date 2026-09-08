# Copyright (c) 2026 Calfus Inc.
# Author: Wasiullah Rafeeq S
# Editor: Prakrit Mohanty

"""Prints the "config created" summary at the end of the init wizard."""

import typer

from cli.status import success, warning
from scanner.base import SCANNER_REGISTRY


def print_summary(
    name: str,
    scanner_type: str,
    scanner_mode: str,
    project_key: str | None,
    sonar_plan: str | None,
    branches: str | None,
    trigger_mode: str,
    ticket_destination_name: str | None = None,
) -> None:
    typer.echo()
    success("Config created:")
    typer.echo(f"  name:           {name}")
    typer.echo(f"  scanner:        {SCANNER_REGISTRY.get(scanner_type, scanner_type)} ({scanner_mode})")
    if project_key:
        typer.echo(f"  project key:    {project_key}")
    if sonar_plan:
        typer.echo(f"  sonar plan:     {sonar_plan}")
    if branches:
        typer.echo(f"  branches:       {branches}")
    typer.echo("  ticket backend: jira")
    if ticket_destination_name:
        typer.echo(f"  ticket dest.:   {ticket_destination_name}")
    typer.echo(f"  trigger mode:   {trigger_mode}")

    if sonar_plan == "free":
        typer.echo()
        warning(
            f"This config needs `gozu run --config {name} --watch` running to actually do anything - "
            "Free plan has no push-based mechanism, so nothing happens automatically on its own."
        )
    elif scanner_type == "trivy":
        typer.echo(
            f"\n`gozu run --config {name} --path <dir>` scans <dir> (defaults to the current directory) "
            "with trivy fs."
        )
    else:
        typer.echo(f"\n`gozu run --config {name}` will use this config.")
