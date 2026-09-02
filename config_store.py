"""
CRUD for the `configs` + `config_credentials` tables (sql/init.sql) - named
sets of scanner/ticket credentials, normalized so a new scanner/ticket
backend never needs a schema change (see sql/init.sql for why).

Every credential value (sonar_token, jira_api_token, webhook_secret, or
whatever a future backend needs) is encrypted via crypto_utils before ever
reaching Postgres, and decrypted only by get_config() - list_configs()
never touches them, since it's for a picker list, not for use.

Called "configs", not "profiles" - see sql/init.sql for why.
"""

import os
from typing import Any

import psycopg
from psycopg.rows import dict_row

from crypto_utils import decrypt_token, encrypt_token


def get_connection() -> psycopg.Connection[dict[str, Any]]:
    return psycopg.connect(
        host=os.environ["POSTGRES_HOST"],
        port=os.environ["POSTGRES_PORT"],
        user=os.environ["POSTGRES_USER"],
        password=os.environ["POSTGRES_PASSWORD"],
        dbname=os.environ["POSTGRES_DB"],
        row_factory=dict_row,
    )


def create_config(
    name: str,
    scanner_type: str,
    scanner_mode: str,
    ticket_backend: str,
    trigger_mode: str,
    credentials: dict[str, str],
) -> int:
    """
    Insert a new config row plus one config_credentials row per entry in
    `credentials` (each value encrypted before insert), in a single
    transaction - if the credentials insert fails, the configs row isn't
    left behind either. Returns the new config's id.
    """
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO configs (name, scanner_type, scanner_mode, ticket_backend, trigger_mode) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (name, scanner_type, scanner_mode, ticket_backend, trigger_mode),
        )
        row = cur.fetchone()
        if row is None:
            raise RuntimeError("INSERT ... RETURNING id returned no row")
        config_id: int = row["id"]

        for key, value in credentials.items():
            cur.execute(
                "INSERT INTO config_credentials (config_id, key, value) VALUES (%s, %s, %s)",
                (config_id, key, encrypt_token(value)),
            )

        return config_id


def get_config(name: str) -> dict | None:
    """
    Look up a config by name: the configs columns at the top level, plus a
    nested "credentials" dict of decrypted key/value pairs. None if not
    found, never raises for a miss.
    """
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM configs WHERE name = %s", (name,))
        config_row = cur.fetchone()
        if config_row is None:
            return None

        cur.execute("SELECT key, value FROM config_credentials WHERE config_id = %s", (config_row["id"],))
        credential_rows = cur.fetchall()

    credentials = {row["key"]: decrypt_token(row["value"]) for row in credential_rows}
    return {**config_row, "credentials": credentials}


def list_configs() -> list[dict]:
    """Config names + non-secret metadata only, for a picker list. No join, no secrets."""
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT name, scanner_type, scanner_mode, trigger_mode FROM configs ORDER BY name")
        return cur.fetchall()


def delete_config(name: str) -> bool:
    """
    Delete a config by name (its config_credentials rows cascade via the
    FK). Returns True if a row was deleted, False if none existed.
    """
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM configs WHERE name = %s", (name,))
        return cur.rowcount > 0


def count_configs() -> int:
    """Plain row count of `configs` - used by bootstrap_env.py's FERNET_KEY safety check."""
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) AS count FROM configs")
        row = cur.fetchone()
        return row["count"] if row is not None else 0
