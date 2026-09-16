# Copyright (c) 2026 Calfus Inc.
# Author: Wasiullah Rafeeq S
#
# Depends on: src/config/store.py - listing configs to decide which service profiles to activate
# Depends on: src/config/migrations.py - applying schema migrations on `gozu up`/`down`
# Depends on: src/cli/config_lookup.py - resolving a config name typed by the user
# Depends on: src/scripts/env_ports.py - reading each service's resolved host port for `gozu ports`
# Depends on: src/cli/stack/files.py - ensure_stack_files() refreshes STACK_DIR's source tree before `gozu up`

"""
`gozu up`/`gozu down` - bringing the local Docker Compose infrastructure
up/down, with which profiles to activate determined by whatever configs
currently exist (see src/cli/init_wizard/ for how those get created). Actual
mechanics live in profiles.py (Compose profile detection), webhooks.py
(secret generation + URL printing), status_report.py (formatted `ps`
output), and wipe.py (`--wipe`'s destructive teardown).
"""

import typer

import config.store as config_store
from cli.config_lookup import resolve_config_or_prompt
from cli.stack.cleanup import InterruptCleanup
from cli.stack.files import ensure_stack_files, require_initialized
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
from cli.status import error, success, waiting, warning
from config.migrations import run_migrations
from scripts.env_ports import DEFAULT_PORTS, ENV_PATH, parse_env_file
from scripts.paths import STACK_DIR

def ensure_config_store_ready() -> None:
    """Any command touching config_store needs a live, migrated Postgres
    first - up()/down() get this for free managing the rest of the stack;
    config-only commands funnel through here instead of repeating the same
    three calls, so a not-yet-up Postgres gets a clear message instead of a
    raw psycopg.OperationalError."""
    require_initialized()
    ensure_postgres_up(STACK_DIR)
    run_migrations(STACK_DIR)


# env var name -> human-readable label, for `gozu ports`. Iterated in
# DEFAULT_PORTS's own order so a port added there can't silently go
# unlabeled here.
_PORT_LABELS: dict[str, str] = {
    "POSTGRES_PORT": "Postgres",
    "SONARQUBE_PORT": "SonarQube",
    "TEMPORAL_PORT": "Temporal (gRPC)",
    "TEMPORAL_UI_PORT": "Temporal UI",
    "RECEIVER_PORT": "Webhook receiver",
}


def up(config: str | None = None) -> None:
    """Brings up Postgres, applies pending migrations, then every
    always-on/profile-active service in one `docker compose up -d`.
    Wrapped in InterruptCleanup so a Ctrl-C/SIGTERM only tears down what
    this invocation itself started.

    `config`, when given, scopes profile activation to that one config
    instead of every config in the store - always-on services start
    either way; only optional profiles (sonarqube-local, webhook) and the
    post-up webhook-URL summary are affected.
    """
    require_initialized()

    # Refreshes STACK_DIR's materialized source tree before every `gozu
    # up` - previously only ran during `gozu init`, so a code change since
    # then had no effect on an already-existing stack until someone
    # happened to re-run init. Idempotent; --build below is what actually
    # turns a refreshed source tree into a rebuilt image.
    ensure_stack_files()

    with InterruptCleanup(STACK_DIR) as cleanup:
        # Tracked BEFORE calling ensure_postgres_up(), not after - an
        # interrupt while still blocked on postgres's healthcheck would
        # otherwise never reach a post-call tracking line.
        if not is_service_up(STACK_DIR, "postgres"):
            cleanup.track("postgres")
        waiting("Bringing up Postgres ...")
        ensure_postgres_up(STACK_DIR, cleanup=cleanup)

        # Migration 0001 creates the schema on a fresh database now, not
        # Postgres's own docker-entrypoint-initdb.d hook.
        waiting("Applying database migrations ...")
        applied = run_migrations(STACK_DIR)
        if applied:
            success(f"Applied {len(applied)} migration(s): {', '.join(applied)}")

        if config:
            configs = [resolve_config_or_prompt(config)]
        else:
            configs = config_store.list_configs()
            if not configs:
                warning("No configs found - run `gozu init` first. Bringing up always-on services only.")

        profiles = active_profiles(configs)

        # One combined `docker compose up -d` gives no per-service "started
        # fresh" signal, so track whatever isn't already up right now,
        # before issuing it - `docker compose stop` on a not-yet-existing
        # container is a no-op either way.
        for service in services_for_profiles(profiles):
            if service != "postgres" and not is_service_up(STACK_DIR, service):
                cleanup.track(service)

        ensure_webhook_secrets(configs)

        # --build: STACK_DIR's source tree was just refreshed above - a
        # no-op for services with no `build:` key (postgres/temporal/
        # sonarqube), cheap when worker/receiver's image is unchanged.
        command = ["docker", "compose", *profile_flags(profiles), "up", "-d", "--build"]
        waiting("Running: " + " ".join(command))
        result = cleanup.run(command, cwd=STACK_DIR)
        if result.returncode != 0:
            error("docker compose up failed.")
            raise typer.Exit(code=result.returncode)

        print_stack_status(STACK_DIR, profiles)

        print_webhook_urls(configs)


def down(wipe: bool = False) -> None:
    """Stop the stack. Plain `down`: containers stop, volumes/data persist.
    `down --wipe`: resets gozu's own database (configs, destinations,
    dedupe claims) - the one irreversible command in this CLI, gated
    behind typing "wipe". Never touches SonarQube's own database/volumes.

    Wrapped in InterruptCleanup since `down` can start Postgres fresh just
    to read configs before it can stop anything."""
    require_initialized()

    with InterruptCleanup(STACK_DIR) as cleanup:
        if not is_service_up(STACK_DIR, "postgres"):
            cleanup.track("postgres")
        ensure_postgres_up(STACK_DIR, cleanup=cleanup)
        configs = config_store.list_configs()
        profiles = active_profiles(configs)

        # Gathered before stop() below, since stop() stops postgres too and
        # counting rows needs a live connection.
        destinations_count = claims_count = 0
        if wipe:
            destinations_count, claims_count = gather_preview()

        stop(profiles, STACK_DIR)

    if not wipe:
        return

    confirm_and_wipe(len(configs), destinations_count, claims_count, STACK_DIR)


def status() -> None:
    """Read-only status check, safe to run any time - no ensure_*_up() call
    anywhere. Uses ALL_PROFILES rather than reading configs from Postgres,
    since Postgres itself might be exactly what's down right now."""
    require_initialized()
    print_stack_status(STACK_DIR, ALL_PROFILES)


def ports() -> None:
    """Prints the real host-side port each service resolved to - bootstrap_env.py
    auto-increments past whatever's already taken, so a service's actual
    port can differ from its documented default."""
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
