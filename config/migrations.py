# Copyright (c) 2026 Calfus Inc.
# Author: Wasiullah Rafeeq S
# Editor: Claude
#
# Depends on: PostgreSQL - data storage
# Depends on: config/connection.py - reads Postgres connection settings to build the Alembic DB URL

"""
The single, authoritative path for creating and evolving gozu's own
database schema, via Alembic (alembic.ini + migrations/ at the repo
root - named migrations/, not alembic/, to avoid a real packaging
collision with the installed `alembic` package itself, see
cli/stack/files.py - materialized into ~/.gozu/stack/ by
cli/stack/files.py the same way docker-compose.yml is). Replaces the old
dual-path setup: sql/init.sql used to run once via Postgres's own
docker-entrypoint-initdb.d hook on a genuinely fresh volume, while
`gozu down --wipe`'s reset separately re-ran that same file by hand
(that hook never fires twice on the same volume). Both `gozu up` (right
after ensure_postgres_up()) and `gozu down --wipe`'s reset (right after
the DROP DATABASE/CREATE DATABASE scoped reset) now call
run_migrations() - one mechanism, not two.

Scope is deliberately narrow: Alembic manages schema evolution ONLY.
config/store.py, config/ticket_destinations.py, and ticket/claims.py all
keep talking to Postgres via raw psycopg exactly as they already do -
no ORM, no SQLAlchemy models anywhere outside this one file. SQLAlchemy
itself is only present at all because Alembic depends on it to drive a
migration's DB connection - every revision under migrations/versions/ is
pure op.execute(<real DDL>), target_metadata is None (see
migrations/env.py), and nothing here ever autogenerates or diffs a
schema.

Invoked via Alembic's own Python API (alembic.command), not a
subprocess shell-out to the `alembic` CLI - this runs inside the same
process gozu itself is running in, so a bad exit code isn't the only
signal of failure; a raised exception here propagates exactly like any
other error in `gozu up`/`gozu down --wipe`.
"""

from pathlib import Path
from urllib.parse import quote_plus

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine

from config.connection import env_settings


def _build_alembic_config(stack_dir: Path) -> Config:
    cfg = Config(str(stack_dir / "alembic.ini"))
    cfg.set_main_option("script_location", str(stack_dir / "migrations"))

    settings = env_settings()
    url = (
        f"postgresql+psycopg://{settings['user']}:{quote_plus(settings['password'])}"
        f"@{settings['host']}:{settings['port']}/{settings['dbname']}"
    )
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def run_migrations(stack_dir: Path) -> list[str]:
    """
    Applies every Alembic revision not yet applied, in order, up to
    head. Returns the revision ids actually applied this call (empty if
    the schema was already fully up to date) - purely for a nicer
    `gozu up`/`gozu down --wipe` status message; Alembic itself is what
    actually tracks/enforces which revisions have run (its own
    `alembic_version` table), not this return value.

    Alembic wraps each revision in its own transaction against a
    transactional-DDL database like Postgres - a failing revision is
    rolled back, not partially applied, and command.upgrade() raises
    rather than silently skipping ahead to the next one.
    """
    cfg = _build_alembic_config(stack_dir)
    script = ScriptDirectory.from_config(cfg)

    engine = create_engine(cfg.get_main_option("sqlalchemy.url"))
    try:
        with engine.connect() as connection:
            current = MigrationContext.configure(connection).get_current_heads()
    finally:
        engine.dispose()

    command.upgrade(cfg, "head")

    target_head = script.get_current_head()
    current_head = current[0] if current else None
    if current_head == target_head:
        return []

    # iterate_revisions(upper, lower) walks newest-first from `upper` back
    # to (excluding) `lower` - reversed so the returned list reads oldest
    # (first applied) to newest, matching the order they were actually run.
    applied = [rev.revision for rev in script.iterate_revisions(target_head, current_head)]
    applied.reverse()
    return applied
