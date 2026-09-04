"""Shared Postgres connection helper - used by config/store.py and config/ticket_destinations.py."""

import os
from typing import Any

import psycopg
from psycopg.rows import dict_row


def get_connection() -> psycopg.Connection[dict[str, Any]]:
    # `psycopg.connect` is `Connection.connect`, a classmethod returning
    # `Self` - calling it unparameterized (the usual `psycopg.connect(...)`)
    # can't infer the row type from `row_factory` through `Self` in every
    # type checker (mypy accepts it; pyright doesn't). Parameterizing
    # `Connection` explicitly before `.connect(...)` resolves `Self`
    # correctly for both.
    return psycopg.Connection[dict[str, Any]].connect(
        host=os.environ["POSTGRES_HOST"],
        port=os.environ["POSTGRES_PORT"],
        user=os.environ["POSTGRES_USER"],
        password=os.environ["POSTGRES_PASSWORD"],
        dbname=os.environ["POSTGRES_DB"],
        row_factory=dict_row,
    )
