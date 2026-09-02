"""
CRUD for the `configs` table (sql/init.sql) - named sets of scanner/ticket
credentials. Secrets (sonar_token, jira_api_token, webhook_secret) are
encrypted via crypto_utils before ever reaching Postgres, and decrypted only
by get_config() - list_configs() never touches them, since it's for a
picker list, not for use.

Called "configs", not "profiles" - see sql/init.sql for why.
"""

import os
from typing import Any

import psycopg
from psycopg.rows import dict_row

from crypto_utils import decrypt_token, encrypt_token

_SECRET_FIELDS = ("sonar_token", "jira_api_token", "webhook_secret")

_CONFIG_FIELDS = (
    "scanner_type",
    "scanner_mode",
    "sonar_host_url",
    "sonar_token",
    "sonar_organization",
    "ticket_backend",
    "jira_url",
    "jira_email",
    "jira_api_token",
    "jira_project_key",
    "trigger_mode",
    "webhook_secret",
)


def get_connection() -> psycopg.Connection:
    return psycopg.connect(
        host=os.environ["POSTGRES_HOST"],
        port=os.environ["POSTGRES_PORT"],
        user=os.environ["POSTGRES_USER"],
        password=os.environ["POSTGRES_PASSWORD"],
        dbname=os.environ["POSTGRES_DB"],
        row_factory=dict_row,
    )


def create_config(name: str, **fields: Any) -> int:
    """
    Insert a new config, encrypting sonar_token/jira_api_token/webhook_secret
    before they ever reach the database. Returns the new row's id.

    Unrecognized keyword arguments raise TypeError (a plain dict.__getitem__
    on a typo'd field name would otherwise silently insert NULL).
    """
    unknown = set(fields) - set(_CONFIG_FIELDS)
    if unknown:
        raise TypeError(f"create_config() got unexpected field(s): {', '.join(sorted(unknown))}")

    values = {field: fields.get(field) for field in _CONFIG_FIELDS}
    for field in _SECRET_FIELDS:
        if values[field] is not None:
            values[field] = encrypt_token(values[field])

    columns = ", ".join(("name", *_CONFIG_FIELDS))
    placeholders = ", ".join(("%(name)s", *(f"%({field})s" for field in _CONFIG_FIELDS)))

    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO configs ({columns}) VALUES ({placeholders}) RETURNING id",
            {"name": name, **values},
        )
        row = cur.fetchone()
        conn.commit()
        return row["id"]


def get_config(name: str) -> dict | None:
    """Look up a config by name, decrypting its secret fields. None if not found."""
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM configs WHERE name = %s", (name,))
        row = cur.fetchone()

    if row is None:
        return None

    for field in _SECRET_FIELDS:
        if row[field] is not None:
            row[field] = decrypt_token(row[field])
    return row


def list_configs() -> list[dict]:
    """Config names + non-secret metadata only, for a picker list."""
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT name, scanner_type, scanner_mode, trigger_mode FROM configs ORDER BY name")
        return cur.fetchall()


def delete_config(name: str) -> bool:
    """Delete a config by name. Returns True if a row was deleted, False if none existed."""
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM configs WHERE name = %s", (name,))
        deleted = cur.rowcount > 0
        conn.commit()
        return deleted
