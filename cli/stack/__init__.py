# Copyright (c) 2026 Calfus Inc.
# Author: Wasiullah Rafeeq S

"""
`gozu up`/`gozu down` - bringing the local Docker Compose infrastructure
up/down, with which profiles to activate determined by whatever configs
currently exist (see cli/init_wizard/ for how those get created). Actual
mechanics live in profiles.py (Compose profile detection), webhooks.py
(secret generation + URL printing), status_report.py (formatted `ps`
output), and wipe.py (`--wipe`'s destructive teardown).
"""

import typer

import config.store as config_store
from cli.stack.cleanup import InterruptCleanup
from cli.stack.files import require_initialized
from cli.stack.profiles import (
    ALL_PROFILES,
    active_profiles,
    ensure_postgres_up,
    is_service_up,
    profile_flags,
    services_for_profiles,
    stop,
)
from cli.stack.status_report import print_stack_status
from cli.stack.webhooks import ensure_webhook_secrets, print_webhook_urls
from cli.stack.wipe import confirm_and_wipe, gather_preview
from cli.status import error, waiting, warning
from scripts.env_ports import DEFAULT_PORTS, ENV_PATH, parse_env_file
from scripts.paths import STACK_DIR

# env var name -> human-readable label, for `gozu ports`. Iterated in
# DEFAULT_PORTS's own order (the authoritative list of ports gozu itself
# manages - see scripts/env_ports.py) rather than hand-maintained
# separately, so a port added there can't silently go unlabeled here; an
# unrecognized name (there shouldn't be one) falls back to itself as the
# label rather than raising.
_PORT_LABELS: dict[str, str] = {
    "POSTGRES_PORT": "Postgres",
    "SONARQUBE_PORT": "SonarQube",
    "TEMPORAL_PORT": "Temporal (gRPC)",
    "TEMPORAL_UI_PORT": "Temporal UI",
    "RECEIVER_PORT": "Webhook receiver",
}


def up() -> None:
    """
    Brings up Postgres (needed just to read configs), then every
    always-on/profile-active service in one `docker compose up -d`.
    Wrapped in InterruptCleanup (cli/stack/cleanup.py) so a Ctrl-C/SIGTERM
    partway through only tears down what THIS invocation itself started -
    whatever was already up and healthy before this ran is left untouched.
    """
    require_initialized()

    with InterruptCleanup(STACK_DIR) as cleanup:
        # Tracked BEFORE calling ensure_postgres_up(), not after it returns
        # - confirmed live that an interrupt landing while still blocked
        # waiting for postgres's own healthcheck never reaches a
        # post-call tracking line at all.
        if not is_service_up(STACK_DIR, "postgres"):
            cleanup.track("postgres")
        waiting("Bringing up Postgres ...")
        ensure_postgres_up(STACK_DIR, cleanup=cleanup)

        configs = config_store.list_configs()
        if not configs:
            warning("No configs found - run `gozu init` first. Bringing up always-on services only.")

        profiles = active_profiles(configs)

        # A single `docker compose up -d` below brings up every
        # not-yet-running candidate service in one shot - there's no
        # natural per-service "started fresh" signal from one combined
        # subprocess call the way ensure_service_up() gives cli/init_wizard/.
        # Instead: whatever isn't already up right now (checked BEFORE
        # issuing that command) is what this invocation is about to start,
        # tracked so an interrupt partway through only stops those. Safe
        # even for one that in fact never got created before the
        # interrupt landed - `docker compose stop` on a not-yet-existing
        # container is a no-op, not an error.
        for service in services_for_profiles(profiles):
            if service != "postgres" and not is_service_up(STACK_DIR, service):
                cleanup.track(service)

        ensure_webhook_secrets(configs)

        command = ["docker", "compose", *profile_flags(profiles), "up", "-d"]
        waiting("Running: " + " ".join(command))
        result = cleanup.run(command, cwd=STACK_DIR)
        if result.returncode != 0:
            error("docker compose up failed.")
            raise typer.Exit(code=result.returncode)

        print_stack_status(STACK_DIR, profiles)

        print_webhook_urls(configs)


def down(wipe: bool = False) -> None:
    """
    Stop the stack. Plain `down`: containers stop (now correctly including
    profile-tagged services like sonarqube-local/webhook) - volumes/data
    persist, same non-destructive behavior as always. `down --wipe`: also
    removes Postgres's (and, if active, SonarQube's) volumes - the one
    irreversible command in this CLI, gated behind typing the literal
    word "wipe".

    Also wrapped in InterruptCleanup: `down` itself can start Postgres
    fresh (same as `up`/`init` - it needs a live connection just to read
    configs before it can stop anything), so an abrupt interrupt between
    that and this function's own stop() call could otherwise leave it
    dangling instead of torn back down.
    """
    require_initialized()

    with InterruptCleanup(STACK_DIR) as cleanup:
        if not is_service_up(STACK_DIR, "postgres"):
            cleanup.track("postgres")
        ensure_postgres_up(STACK_DIR, cleanup=cleanup)
        configs = config_store.list_configs()
        profiles = active_profiles(configs)

        # Gathered before stop() below runs, deliberately - stop() stops
        # postgres along with everything else, and counting rows needs a
        # live connection.
        destinations_count = claims_count = 0
        wipes_sonarqube = False
        if wipe:
            destinations_count, claims_count, wipes_sonarqube = gather_preview(profiles)

        stop(profiles, STACK_DIR)
        # stop() above already stopped whatever ensure_postgres_up started
        # (if anything) as part of down()'s own normal completion - once
        # we're here, there's nothing left for an interrupt during the
        # --wipe confirmation prompt below (outside this `with` block) to
        # need cleaning up.

    if not wipe:
        return

    confirm_and_wipe(profiles, len(configs), destinations_count, claims_count, wipes_sonarqube, STACK_DIR)


def status() -> None:
    """
    Read-only status check: prints every service's current state (never
    created / stopped / starting / healthy / unhealthy) without bringing
    anything up - no ensure_*_up() call anywhere in this function, so it's
    safe to run any time, including when nothing at all is running yet.

    Uses ALL_PROFILES (every service gozu could ever manage), not just
    the ones this machine's current configs happen to need - deliberately
    doesn't read configs from Postgres to narrow that down, since
    Postgres itself might be exactly the thing that's down right now, and
    this command must still work in that case.
    """
    require_initialized()
    print_stack_status(STACK_DIR, ALL_PROFILES)


def ports() -> None:
    """
    Prints the real host-side port each service resolved to, straight
    from ~/.gozu/stack/.env - bootstrap_env.py auto-increments past
    whatever's already taken on this machine at `gozu init` time, so a
    service's actual port can differ from its documented default (e.g.
    SonarQube landing on 9001 because something else already had 9000).
    """
    require_initialized()
    values = parse_env_file(ENV_PATH)
    typer.echo("Ports:")
    for name in DEFAULT_PORTS:
        label = _PORT_LABELS.get(name, name)
        port = values.get(name)
        if port:
            typer.echo(f"  {label:<20} {port}")
        else:
            warning(f"  {label:<20} not set in .env")
