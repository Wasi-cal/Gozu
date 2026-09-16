# Copyright (c) 2026 Calfus Inc.
# Author: Wasiullah Rafeeq S
#
# Depends on: PostgreSQL - data storage

"""Shared Postgres connection helper - used by src/config/store.py and src/config/ticket_destinations.py."""

import os
from typing import Any

import psycopg
from psycopg.rows import dict_row


def env_settings() -> dict[str, str]:
    """The same env vars get_connection() reads, as a plain dict - pulled
    out so src/config/migrations.py can build a SQLAlchemy URL from the
    same values without duplicating the lookup."""
    return {
        "host": os.environ["POSTGRES_HOST"],
        "port": os.environ["POSTGRES_PORT"],
        "user": os.environ["POSTGRES_USER"],
        "password": os.environ["POSTGRES_PASSWORD"],
        "dbname": os.environ["POSTGRES_DB"],
    }


def get_connection() -> psycopg.Connection[dict[str, Any]]:
    # Parameterizing `Connection` explicitly resolves the row_factory type
    # for pyright, which `psycopg.connect(...)` unparameterized can't infer.
    # Named individually, not `**env_settings()`, so pyright checks each
    # key against its own parameter rather than every kwarg connect() takes.
    settings = env_settings()
    return psycopg.Connection[dict[str, Any]].connect(
        host=settings["host"],
        port=settings["port"],
        user=settings["user"],
        password=settings["password"],
        dbname=settings["dbname"],
        row_factory=dict_row,
    )
