# Copyright (c) 2026 Calfus Inc.
# Author: Wasiullah Rafeeq S

"""`gozu down --wipe`'s destructive teardown: gathering what would be destroyed, confirming, then actually removing it."""

import subprocess
from pathlib import Path

import questionary
import typer

import config.store as config_store
from cli.stack.backup import create_backup, next_backup_path
from cli.stack.profiles import ensure_postgres_up, profile_flags


def gather_preview(profiles: set[str]) -> tuple[int, int, bool]:
    """
    Must be called BEFORE stopping postgres (see cli/stack/__init__.py's
    down()) - counting rows needs a live connection, and stopping the
    stack stops postgres along with everything else.
    """
    destinations_count = len(config_store.list_ticket_destinations())
    with config_store.get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) AS count FROM ticket_claims")
        row = cur.fetchone()
        claims_count = row["count"] if row else 0
    wipes_sonarqube = "sonarqube-local" in profiles
    return destinations_count, claims_count, wipes_sonarqube


def confirm_and_wipe(
    profiles: set[str],
    configs_count: int,
    destinations_count: int,
    claims_count: int,
    wipes_sonarqube: bool,
    stack_dir: Path,
) -> None:
    backup_target = next_backup_path()

    typer.secho("\nThis will PERMANENTLY delete:", fg=typer.colors.RED, bold=True)
    typer.echo(f"  {configs_count} config(s)")
    typer.echo(f"  {destinations_count} ticket destination(s)")
    typer.echo(f"  {claims_count} dedupe claim(s) (ticket_claims)")
    if wipes_sonarqube:
        typer.secho(
            "  local SonarQube's scan history/volume too - sonarqube-local is an active profile this run",
            fg=typer.colors.RED,
        )
    else:
        typer.echo("  (local SonarQube's volume will NOT be touched - sonarqube-local isn't an active profile)")
    typer.echo(f"\nA Postgres backup will be written to {backup_target} before anything is deleted.")

    answer = questionary.text("\nType 'wipe' to confirm - anything else cancels:").ask()
    if answer != "wipe":
        typer.secho(
            f"Cancelled ({answer!r} != 'wipe') - nothing was deleted; the stack is stopped, not wiped.",
            fg=typer.colors.YELLOW,
        )
        return

    # down() already stopped postgres as part of the plain-stop step above -
    # briefly bring it back so pg_dump has something to actually connect to;
    # the docker compose down -v below removes it for real either way.
    ensure_postgres_up(stack_dir)
    typer.echo("Backing up Postgres before wiping ...")
    create_backup(backup_target, stack_dir)
    typer.secho(f"Backup written: {backup_target}", fg=typer.colors.GREEN)

    command = ["docker", "compose", *profile_flags(profiles), "down", "-v"]
    typer.echo("Running: " + " ".join(command))
    result = subprocess.run(command, check=False, cwd=stack_dir)
    if result.returncode != 0:
        raise typer.Exit(code=result.returncode)
    typer.secho("Stack wiped - configs, ticket destinations, dedupe claims, and volumes are gone.", fg=typer.colors.RED)
