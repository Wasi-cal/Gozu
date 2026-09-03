"""Prints the "config created" summary at the end of the init wizard."""

import typer

from scanner.base import SCANNER_REGISTRY


def print_summary(
    name: str,
    scanner_type: str,
    scanner_mode: str,
    project_key: str,
    sonar_plan: str | None,
    branches: str | None,
    trigger_mode: str,
) -> None:
    typer.secho("\nConfig created:", bold=True, fg=typer.colors.GREEN)
    typer.echo(f"  name:           {name}")
    typer.echo(f"  scanner:        {SCANNER_REGISTRY.get(scanner_type, scanner_type)} ({scanner_mode})")
    typer.echo(f"  project key:    {project_key}")
    if sonar_plan:
        typer.echo(f"  sonar plan:     {sonar_plan}")
    if branches:
        typer.echo(f"  branches:       {branches}")
    typer.echo("  ticket backend: jira")
    typer.echo(f"  trigger mode:   {trigger_mode}")

    if sonar_plan == "free":
        typer.secho(
            f"\nThis config needs `codescan run --config {name} --watch` running to actually do anything - "
            "Free plan has no push-based mechanism, so nothing happens automatically on its own.",
            fg=typer.colors.YELLOW,
        )
    else:
        typer.echo(f"\n`codescan run --config {name}` will use this config.")
