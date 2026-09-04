# Copyright (c) 2026 Calfus Inc.
# Author: Wasiullah Rafeeq S
#
# Depends on: PostgreSQL - data storage

"""
CRUD for the `ticket_destinations` + `ticket_destination_credentials`
tables (sql/init.sql) - a shared ticket board + credentials that more than
one config can reference (`configs.ticket_destination_id`), instead of
each config carrying its own copy of the same credentials.

Same encrypt-on-write/decrypt-on-read shape as config/store.py's configs/
config_credentials - kept in its own module since it's a genuinely
separate set of tables, not because the logic differs.
"""

from config.connection import get_connection
from config.crypto import decrypt_token, encrypt_token


def create_ticket_destination(
    name: str, ticket_backend: str, project_key: str, credentials: dict[str, str]
) -> int:
    """
    Insert a new ticket_destinations row plus one ticket_destination_credentials
    row per entry in `credentials` (each value encrypted before insert), in
    a single transaction. Returns the new destination's id.
    """
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO ticket_destinations (name, ticket_backend, project_key) VALUES (%s, %s, %s) RETURNING id",
            (name, ticket_backend, project_key),
        )
        row = cur.fetchone()
        if row is None:
            raise RuntimeError("INSERT ... RETURNING id returned no row")
        destination_id: int = row["id"]

        for key, value in credentials.items():
            cur.execute(
                "INSERT INTO ticket_destination_credentials (destination_id, key, value) VALUES (%s, %s, %s)",
                (destination_id, key, encrypt_token(value)),
            )

        return destination_id


def get_ticket_destination_by_id(destination_id: int) -> dict | None:
    """Same decrypted-credentials shape as get_config() - used by config/store.py's get_config() to resolve a config's Jira fields."""
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM ticket_destinations WHERE id = %s", (destination_id,))
        destination_row = cur.fetchone()
        if destination_row is None:
            return None

        cur.execute(
            "SELECT key, value FROM ticket_destination_credentials WHERE destination_id = %s", (destination_id,)
        )
        credential_rows = cur.fetchall()

    credentials = {row["key"]: decrypt_token(row["value"]) for row in credential_rows}
    return {**destination_row, "credentials": credentials}


def get_ticket_destination(name: str) -> dict | None:
    """Look up a ticket destination by name - None if it doesn't exist, never raises for a miss."""
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT id FROM ticket_destinations WHERE name = %s", (name,))
        row = cur.fetchone()
    return get_ticket_destination_by_id(row["id"]) if row else None


def list_ticket_destinations() -> list[dict]:
    """Destination names + non-secret metadata only, for a picker list. No join, no secrets."""
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT name, ticket_backend, project_key FROM ticket_destinations ORDER BY name")
        return cur.fetchall()
