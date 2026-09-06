# Copyright (c) 2026 Calfus Inc.
# Author: Wasiullah Rafeeq S

"""
Compose profile detection + the docker-compose invocations that need it -
shared by `gozu up` (what to start) and `gozu down` (what to stop), so
they can never disagree about what's actually part of "the stack".
"""

import json
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import typer

from cli.status import error

if TYPE_CHECKING:
    # String-quoted forward reference only (see call sites below) - a real
    # top-level import here would be circular, since cli/stack/cleanup.py
    # itself imports stop() from this module.
    from cli.stack.cleanup import InterruptCleanup

# Every profile any service in docker-compose.yml is gated behind - used
# when targeting specific services by name (see stop()'s `services` param)
# where the caller doesn't necessarily know which profile each belongs to.
# Confirmed live (docker compose v5.3.1): --profile flags for profiles
# that don't apply to any of the named services are harmless no-ops, so
# passing every known one here is safe regardless of which named services
# are actually being targeted.
ALL_PROFILES = {"sonarqube-local", "webhook"}

# Always-on services (no `profiles:` key in docker-compose.yml at all) -
# distinct from the profile-gated ones added conditionally in
# services_for_profiles() below.
_ALWAYS_ON_SERVICES = ["postgres", "temporal", "worker"]


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


def services_for_profiles(profiles: set[str]) -> list[str]:
    """
    Concrete service names docker-compose.yml would actually bring up for
    `profiles` - the always-on ones plus whichever profile-gated ones are
    active. Mirrors docker-compose.yml exactly: postgres/temporal/worker
    have no `profiles:` key at all; sonarqube is "sonarqube-local";
    receiver is "webhook".
    """
    services = list(_ALWAYS_ON_SERVICES)
    if "sonarqube-local" in profiles:
        services.append("sonarqube")
    if "webhook" in profiles:
        services.append("receiver")
    return services


def _service_state(stack_dir: Path, service: str) -> dict | None:
    """
    Compose's own record of `service`'s single container, or None if it's
    never been created at all. Confirmed live: `ps --all --format json
    <service>` needs no --profile flag even for a profile-gated service
    (naming a service explicitly is enough for compose to look it up,
    profile filtering only matters for commands deciding what to create),
    and returns empty stdout (not an error) for a real, compose-file-defined
    service that just doesn't have a container yet.
    """
    result = subprocess.run(
        ["docker", "compose", "ps", "--all", "--format", "json", service],
        capture_output=True,
        text=True,
        check=False,
        cwd=stack_dir,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None
    # One JSON object per line (not a single JSON array) - only ever one
    # line here since a service name is always exactly one container in
    # this project's compose file (no `replicas`/scale usage anywhere).
    return json.loads(result.stdout.strip().splitlines()[0])


def is_service_up(stack_dir: Path, service: str) -> bool:
    """
    True if `service` is already running AND (healthy, or has no
    healthcheck defined at all - e.g. `worker`, which docker-compose.yml
    gives no HEALTHCHECK, so "running" is the best signal available).
    False for anything else: never created, exited, or currently
    unhealthy/still starting.
    """
    state = _service_state(stack_dir, service)
    if state is None or state.get("State") != "running":
        return False
    return state.get("Health", "") in ("", "healthy")


def ensure_service_up(
    stack_dir: Path, service: str, profile: str | None = None, cleanup: "InterruptCleanup | None" = None
) -> bool:
    """
    Bring up one Compose service and wait for its healthcheck, regardless
    of what else is running - a no-op if it's already up and healthy.
    `stack_dir` is the materialized docker-compose.yml's directory (see
    cli/stack/files.py's ensure_stack_files()) - `docker compose` resolves
    the compose file from cwd, not this file's location. `profile` is
    needed for services gated behind one (e.g. sonarqube-local) - without
    it, `up` can't see the service at all, profile or not.

    Returns True if this call actually started the service fresh, False if
    it was already up and healthy beforehand - callers use this to track
    "did *this* invocation bring this up" for scoped SIGINT/SIGTERM
    cleanup (see cli/stack/cleanup.py), so an abrupt interrupt only tears
    down what it itself started, never a service that predates it.

    `cleanup`, when given, routes the actual subprocess call through its
    `run()` instead of a plain subprocess.run() - so an interrupt landing
    while this is still blocked waiting on the healthcheck can terminate
    this specific child directly, not just abandon Python's own wait on it.
    """
    if is_service_up(stack_dir, service):
        return False

    command = ["docker", "compose"]
    if profile:
        command += ["--profile", profile]
    command += ["up", "-d", "--wait", service]
    result = cleanup.run(command, cwd=stack_dir) if cleanup is not None else subprocess.run(command, check=False, cwd=stack_dir)
    if result.returncode != 0:
        error(f"Failed to start {service}.")
        raise typer.Exit(code=result.returncode)
    return True


def ensure_postgres_up(stack_dir: Path, cleanup: "InterruptCleanup | None" = None) -> bool:
    """
    Reading configs needs a live Postgres connection - but postgres is
    itself one of the services `up`/`down` manage, so it has to come up
    before any config_store call. Returns whether this call started it
    fresh (see ensure_service_up()).
    """
    return ensure_service_up(stack_dir, "postgres", cleanup=cleanup)


def stop(profiles: set[str], stack_dir: Path, services: list[str] | None = None) -> None:
    """
    Stop the stack - or, if `services` is given, only those specific
    named services (see cli/stack/cleanup.py's scoped interrupt cleanup,
    which only ever wants to stop what one invocation itself started
    fresh, not everything currently active). `profiles` is still passed
    through either way for consistency, but confirmed live it's not
    actually required to target an already-created service by explicit
    name - `docker compose stop <service>` works with no --profile flag
    at all, even for a profile-gated one.
    """
    command = ["docker", "compose", *profile_flags(profiles), "stop"]
    if services:
        command += services
    typer.echo("Running: " + " ".join(command))
    result = subprocess.run(command, check=False, cwd=stack_dir)
    if result.returncode != 0:
        raise typer.Exit(code=result.returncode)
