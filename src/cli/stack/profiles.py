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
    # top-level import here would be circular, since src/cli/stack/cleanup.py
    # itself imports stop() from this module.
    from cli.stack.cleanup import InterruptCleanup

# Every profile any service in docker-compose.yml is gated behind - a
# --profile flag for one that doesn't apply to a named service is a
# harmless no-op, so passing every known one here is always safe.
ALL_PROFILES = {"sonarqube-local", "webhook"}

# Always-on services (no `profiles:` key in docker-compose.yml at all) -
# distinct from the profile-gated ones added conditionally in
# services_for_profiles() below.
_ALWAYS_ON_SERVICES = ["postgres", "temporal", "worker"]


def active_profiles(configs: list[dict]) -> set[str]:
    """Which Compose profiles are actually in play right now, per current
    configs. Both `up` and `down` need this - a bare `docker compose stop`
    with no --profile flags can't see profile-tagged services at all."""
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
    """Concrete service names docker-compose.yml would bring up for
    `profiles` - always-on ones plus whichever profile-gated ones are active."""
    services = list(_ALWAYS_ON_SERVICES)
    if "sonarqube-local" in profiles:
        services.append("sonarqube")
    if "webhook" in profiles:
        services.append("receiver")
    return services


def service_state(stack_dir: Path, service: str) -> dict | None:
    """Compose's own record of `service`'s single container, or None if
    it's never been created. Needs no --profile flag even for a
    profile-gated service; returns empty stdout (not an error) for one
    with no container yet."""
    result = subprocess.run(
        ["docker", "compose", "ps", "--all", "--format", "json", service],
        capture_output=True,
        text=True,
        check=False,
        cwd=stack_dir,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None
    # One JSON object per line, always exactly one since no service uses
    # replicas/scale in this project's compose file.
    return json.loads(result.stdout.strip().splitlines()[0])


def is_service_up(stack_dir: Path, service: str) -> bool:
    """True if `service` is running AND (healthy, or has no healthcheck
    defined at all - e.g. `worker`, where "running" is the best signal)."""
    state = service_state(stack_dir, service)
    if state is None or state.get("State") != "running":
        return False
    return state.get("Health", "") in ("", "healthy")


def ensure_service_up(
    stack_dir: Path, service: str, profile: str | None = None, cleanup: "InterruptCleanup | None" = None
) -> bool:
    """Bring up one Compose service and wait for its healthcheck - a no-op
    if already up and healthy. `profile` is needed for gated services,
    without which `up` can't see the service at all.

    Returns True if this call started the service fresh, False if it was
    already up - callers use this for scoped SIGINT/SIGTERM cleanup, so an
    interrupt only tears down what it itself started.

    `cleanup`, when given, routes the subprocess call through its `run()`
    so an interrupt while waiting on the healthcheck can terminate this
    specific child directly."""
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
    """Postgres has to come up before any config_store call. Returns
    whether this call started it fresh (see ensure_service_up())."""
    return ensure_service_up(stack_dir, "postgres", cleanup=cleanup)


def stop(profiles: set[str], stack_dir: Path, services: list[str] | None = None) -> None:
    """Stop the stack - or, if `services` is given, only those specific
    services (for scoped interrupt cleanup). `docker compose stop
    <service>` works with no --profile flag even for a gated one."""
    command = ["docker", "compose", *profile_flags(profiles), "stop"]
    if services:
        command += services
    typer.echo("Running: " + " ".join(command))
    result = subprocess.run(command, check=False, cwd=stack_dir)
    if result.returncode != 0:
        raise typer.Exit(code=result.returncode)
