# Copyright (c) 2026 Calfus Inc.
# Author: Wasiullah Rafeeq S

"""
Compose profile detection + the docker-compose invocations that need it -
shared by `gozu up` (what to start) and `gozu down` (what to stop), so
they can never disagree about what's actually part of "the stack".
"""

import subprocess

import typer


def active_profiles(configs: list[dict]) -> set[str]:
    """
    Which Compose profiles are actually in play right now, per current
    configs. Previously only `up` computed this; `down` ran a bare
    `docker compose stop` with no --profile flags, which can't see
    profile-tagged services (sonarqube-local, webhook) at all - they'd
    stay running after `down`.
    """
    profiles = set()
    for config in configs:
        if config["scanner_mode"] == "local":
            profiles.add("sonarqube-local")
        if config["trigger_mode"] == "webhook":
            profiles.add("webhook")
    return profiles


def profile_flags(profiles: set[str]) -> list[str]:
    """--profile is a top-level `docker compose` flag, not an `up`/`down`/`stop` option - it has to come before the subcommand."""
    flags = []
    for profile in sorted(profiles):
        flags += ["--profile", profile]
    return flags


def ensure_postgres_up() -> None:
    """
    Reading configs needs a live Postgres connection - but postgres is
    itself one of the services `up`/`down` manage, so it has to come up
    (and actually finish its healthcheck, not just start the container)
    before any config_store call. A no-op if it's already up and healthy.
    """
    result = subprocess.run(["docker", "compose", "up", "-d", "--wait", "postgres"], check=False)
    if result.returncode != 0:
        typer.secho("Failed to start postgres.", fg=typer.colors.RED)
        raise typer.Exit(code=result.returncode)


def stop(profiles: set[str]) -> None:
    command = ["docker", "compose", *profile_flags(profiles), "stop"]
    typer.echo("Running: " + " ".join(command))
    result = subprocess.run(command, check=False)
    if result.returncode != 0:
        raise typer.Exit(code=result.returncode)
