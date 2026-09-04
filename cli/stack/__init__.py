# Copyright (c) 2026 Calfus Inc.
# Author: Wasiullah Rafeeq S

"""
`gozu up`/`gozu down` - bringing the local Docker Compose infrastructure
up/down, with which profiles to activate determined by whatever configs
currently exist (see cli/init_wizard/ for how those get created). Actual
mechanics live in profiles.py (Compose profile detection), webhooks.py
(secret generation + URL printing), and wipe.py (`--wipe`'s destructive
teardown).
"""

import subprocess

import typer

import config.store as config_store
from cli.stack.profiles import active_profiles, ensure_postgres_up, profile_flags, stop
from cli.stack.webhooks import ensure_webhook_secrets, print_webhook_urls
from cli.stack.wipe import confirm_and_wipe, gather_preview


def up() -> None:
    ensure_postgres_up()

    configs = config_store.list_configs()
    if not configs:
        typer.secho(
            "No configs found - run `gozu init` first. Bringing up always-on services only.",
            fg=typer.colors.YELLOW,
        )

    profiles = active_profiles(configs)
    ensure_webhook_secrets(configs)

    command = ["docker", "compose", *profile_flags(profiles), "up", "-d"]
    typer.echo("Running: " + " ".join(command))
    result = subprocess.run(command, check=False)
    if result.returncode != 0:
        typer.secho("docker compose up failed.", fg=typer.colors.RED)
        raise typer.Exit(code=result.returncode)

    typer.echo("\nStatus:")
    subprocess.run(["docker", "compose", "ps"], check=False)
    if "sonarqube-local" in profiles:
        typer.secho(
            "\nsonarqube can take 30-60s+ to show healthy - check `docker compose ps` again shortly.", dim=True
        )

    print_webhook_urls(configs)


def down(wipe: bool = False) -> None:
    """
    Stop the stack. Plain `down`: containers stop (now correctly including
    profile-tagged services like sonarqube-local/webhook) - volumes/data
    persist, same non-destructive behavior as always. `down --wipe`: also
    removes Postgres's (and, if active, SonarQube's) volumes - the one
    irreversible command in this CLI, gated behind typing the literal
    word "wipe".
    """
    ensure_postgres_up()
    configs = config_store.list_configs()
    profiles = active_profiles(configs)

    # Gathered before stop() below runs, deliberately - stop() stops
    # postgres along with everything else, and counting rows needs a
    # live connection.
    destinations_count = claims_count = 0
    wipes_sonarqube = False
    if wipe:
        destinations_count, claims_count, wipes_sonarqube = gather_preview(profiles)

    stop(profiles)

    if not wipe:
        return

    confirm_and_wipe(profiles, len(configs), destinations_count, claims_count, wipes_sonarqube)
