# Copyright (c) 2026 Calfus Inc.
# Author: Wasiullah Rafeeq S
#
# Depends on: PostgreSQL - data storage
# Depends on: config/store.py - counting/reading rows before the destructive reset
# Depends on: config/migrations.py - reapplying schema migrations after the reset
# Depends on: cli/stack/backup.py - backing up the database before wiping it
# Depends on: cli/stack/profiles.py - bringing Postgres up and stopping services around the wipe

"""`gozu down --wipe`'s destructive teardown: gathering what would be reset, confirming, then actually resetting it."""

import os
import subprocess
from pathlib import Path

import questionary
import typer

import config.store as config_store
from cli.stack.backup import create_backup, next_backup_path
from cli.stack.profiles import ensure_postgres_up, stop
from cli.status import success, waiting, warning
from config.migrations import run_migrations


def gather_preview() -> tuple[int, int]:
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
    return destinations_count, claims_count


def _run_psql(stack_dir: Path, dbname: str, sql: str) -> None:
    """Runs `sql` against `dbname` inside the running postgres container, as POSTGRES_USER (the cluster superuser in the official postgres image, same as config/connection.py assumes)."""
    command = [
        "docker",
        "compose",
        "exec",
        "-T",
        "postgres",
        "psql",
        "-v",
        "ON_ERROR_STOP=1",
        "-U",
        os.environ["POSTGRES_USER"],
        "-d",
        dbname,
    ]
    result = subprocess.run(command, input=sql, capture_output=True, text=True, check=False, cwd=stack_dir)
    if result.returncode != 0:
        raise RuntimeError(f"psql against '{dbname}' failed: {result.stderr}")


def reset_gozu_database(stack_dir: Path) -> None:
    """
    Resets ONLY gozu's own database: DROP + CREATE, then runs every
    Alembic revision (config/migrations.py's run_migrations(), alembic/
    versions/) to recreate its schema from scratch against the freshly
    emptied database - the same call `gozu up` makes on a fresh install,
    not a separate re-run of a single init.sql file. Replaces the old
    blanket `docker compose down -v`,
    which destroyed every database in the Postgres instance (and the
    instance's container/volume itself) - this never touches the separate
    `sonarqube` database (sql/init_sonarqube_db.sql) or the postgres_data
    volume as a whole, which is exactly why local-mode SonarQube's own
    Postgres-backed history now survives a wipe untouched, now that it
    lives in the same instance instead of a second container.
    """
    postgres_user = os.environ["POSTGRES_USER"]
    postgres_db = os.environ["POSTGRES_DB"]

    # Can't DROP DATABASE while connected to it, or while anything else
    # is - connect to Postgres's own always-present "postgres" maintenance
    # database instead to terminate other connections and do the
    # drop/recreate. Identifiers interpolated directly (not parameterized)
    # since DDL can't take bind params for object names anyway - both
    # values come from this machine's own .env, never remote/untrusted
    # input, same trust boundary create_backup() already assumes for the
    # same two values.
    _run_psql(
        stack_dir,
        "postgres",
        f"""
        SELECT pg_terminate_backend(pid) FROM pg_stat_activity
            WHERE datname = '{postgres_db}' AND pid <> pg_backend_pid();
        DROP DATABASE IF EXISTS "{postgres_db}";
        CREATE DATABASE "{postgres_db}" OWNER "{postgres_user}";
        """,
    )

    # One mechanism, not two: the same run_migrations() `gozu up` calls on
    # a fresh install recreates the schema here too - the first Alembic
    # revision IS the fresh-install case, so a wiped database ends up on
    # the exact same schema as a genuinely fresh one, not a
    # separately-maintained copy of it.
    run_migrations(stack_dir)


def confirm_and_wipe(
    configs_count: int,
    destinations_count: int,
    claims_count: int,
    stack_dir: Path,
) -> None:
    backup_target = next_backup_path()

    warning("This will PERMANENTLY reset gozu's own database (SonarQube's own database is untouched):")
    typer.echo(f"  {configs_count} config(s)")
    typer.echo(f"  {destinations_count} ticket destination(s)")
    typer.echo(f"  {claims_count} dedupe claim(s) (ticket_claims)")
    typer.echo(f"\nA Postgres backup of gozu's own database will be written to {backup_target} before anything is reset.")

    answer = questionary.text("\nType 'wipe' to confirm - anything else cancels:").ask()
    if answer != "wipe":
        warning(f"Cancelled ({answer!r} != 'wipe') - nothing was reset; the stack is stopped, not wiped.")
        return

    # down() already stopped postgres as part of the plain-stop step above -
    # briefly bring it back so pg_dump/psql have something to actually
    # connect to.
    ensure_postgres_up(stack_dir)
    waiting("Backing up gozu's database before resetting it ...")
    create_backup(backup_target, stack_dir)
    success(f"Backup written: {backup_target}")

    waiting("Resetting gozu's own database (SonarQube's database is untouched) ...")
    reset_gozu_database(stack_dir)
    success("gozu's database has been reset - configs, ticket destinations, and dedupe claims are gone.")

    waiting("Stopping Postgres ...")
    stop(set(), stack_dir, services=["postgres"])
