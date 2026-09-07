# Copyright (c) 2026 Calfus Inc.
# Author: Wasiullah Rafeeq S
#
# Depends on: PostgreSQL - data storage

"""
Pure raw-SQL migrations - target_metadata stays None deliberately, since
there's no ORM/SQLAlchemy model layer anywhere in this codebase to diff
against (config_store.py/ticket_destinations.py/claims.py all stay on
raw psycopg - see CLAUDE.md). Every revision under versions/ calls
op.execute() with real DDL, never op.create_table()/autogenerate.

sqlalchemy.url resolution has two paths:
  - config/migrations.py's run_migrations() (the `gozu up`/`gozu down
    --wipe` path) sets it explicitly via Config.set_main_option() before
    invoking Alembic's Python API - that value wins here.
  - Run directly (`uv run alembic upgrade head` from a repo checkout, for
    hand-authoring/testing a new revision), alembic.ini has no hardcoded
    URL at all - falls back to building one from this process's own
    environment (.env, loaded the same way the rest of the CLI does),
    so a developer never has to hand-maintain a second copy of Postgres
    credentials just for Alembic.
"""

from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = None


def _resolve_url() -> str:
    url = config.get_main_option("sqlalchemy.url")
    if url:
        return url

    from urllib.parse import quote_plus

    from scripts.bootstrap_env import load_into_environ

    load_into_environ()
    import os

    user = os.environ["POSTGRES_USER"]
    password = quote_plus(os.environ["POSTGRES_PASSWORD"])
    host = os.environ["POSTGRES_HOST"]
    port = os.environ["POSTGRES_PORT"]
    dbname = os.environ["POSTGRES_DB"]
    return f"postgresql+psycopg://{user}:{password}@{host}:{port}/{dbname}"


def run_migrations_offline() -> None:
    context.configure(
        url=_resolve_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _resolve_url()
    connectable = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
