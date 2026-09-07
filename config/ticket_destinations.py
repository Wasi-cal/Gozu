# Copyright (c) 2026 Calfus Inc.
# Author: Wasiullah Rafeeq S
#
# Depends on: PostgreSQL - data storage
# Depends on: config/connection.py - gets a Postgres connection
# Depends on: config/crypto.py - encrypts/decrypts credential values

"""
CRUD for the `ticket_destinations` + `ticket_destination_credentials`
tables (migrations/versions/0005_add_ticket_destinations.py) - a shared
ticket board + credentials that more than one config can reference
(`configs.ticket_destination_id`), instead of each config carrying its
own copy of the same credentials.

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


def set_destination_credential(destination_id: int, key: str, value: str) -> None:
    """Insert or update one credential on a ticket destination, encrypting `value` first - same shape as config/store.py's set_credential()."""
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO ticket_destination_credentials (destination_id, key, value) VALUES (%s, %s, %s) "
            "ON CONFLICT (destination_id, key) DO UPDATE SET value = EXCLUDED.value",
            (destination_id, key, encrypt_token(value)),
        )


def set_destination_project_key(destination_id: int, project_key: str) -> None:
    """
    project_key lives as a plain column on ticket_destinations, not a
    ticket_destination_credentials row (it isn't a secret - see
    create_ticket_destination()) - config/store.py's get_config() reads it
    as `credentials["jira_project_key"]` for any config referencing this
    destination, but it's never encrypted or stored as a credential key.
    """
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("UPDATE ticket_destinations SET project_key = %s WHERE id = %s", (project_key, destination_id))


def count_configs_using_destination(destination_id: int, exclude_config_name: str | None = None) -> int:
    """
    How many configs reference this destination - used before writing a
    shared credential/project_key change, so a caller (gozu config edit)
    can warn "this affects N other config(s)" rather than silently
    mutating state other configs also depend on. `exclude_config_name`
    leaves the config actually being edited out of its own count.
    """
    with get_connection() as conn, conn.cursor() as cur:
        if exclude_config_name is not None:
            cur.execute(
                "SELECT count(*) AS count FROM configs WHERE ticket_destination_id = %s AND name != %s",
                (destination_id, exclude_config_name),
            )
        else:
            cur.execute("SELECT count(*) AS count FROM configs WHERE ticket_destination_id = %s", (destination_id,))
        row = cur.fetchone()
        return row["count"] if row else 0
