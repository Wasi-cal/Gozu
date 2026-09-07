# Copyright (c) 2026 Calfus Inc.
# Author: Wasiullah Rafeeq S
#
# Depends on: PostgreSQL - data storage

"""Shared Postgres connection helper - used by config/store.py and config/ticket_destinations.py."""

import os
from typing import Any

import psycopg
from psycopg.rows import dict_row


def env_settings() -> dict[str, str]:
    """
    The same POSTGRES_HOST/PORT/USER/PASSWORD/DB env vars get_connection()
    reads, as a plain dict - pulled out so config/migrations.py (which
    needs these same five values to build a SQLAlchemy URL for Alembic,
    not a psycopg connection) doesn't duplicate this exact env-var
    lookup a second time.
    """
    return {
        "host": os.environ["POSTGRES_HOST"],
        "port": os.environ["POSTGRES_PORT"],
        "user": os.environ["POSTGRES_USER"],
        "password": os.environ["POSTGRES_PASSWORD"],
        "dbname": os.environ["POSTGRES_DB"],
    }


def get_connection() -> psycopg.Connection[dict[str, Any]]:
    # `psycopg.connect` is `Connection.connect`, a classmethod returning
    # `Self` - calling it unparameterized (the usual `psycopg.connect(...)`)
    # can't infer the row type from `row_factory` through `Self` in every
    # type checker (mypy accepts it; pyright doesn't). Parameterizing
    # `Connection` explicitly before `.connect(...)` resolves `Self`
    # correctly for both.
    #
    # Named individually rather than `**env_settings()` - spreading a
    # dict[str, str] makes pyright check that `str` against every other
    # keyword param `connect()` accepts (autocommit: bool,
    # prepare_threshold: int | None, context: AdaptContext | None, ...),
    # since any of those could in principle be filled from an arbitrary
    # str key in the dict. Passing the five keys by name only checks
    # each against its own (str-compatible) parameter.
    settings = env_settings()
    return psycopg.Connection[dict[str, Any]].connect(
        host=settings["host"],
        port=settings["port"],
        user=settings["user"],
        password=settings["password"],
        dbname=settings["dbname"],
        row_factory=dict_row,
    )
