# Copyright (c) 2026 Calfus Inc.
# Author: Wasiullah Rafeeq S
# Editor: Prakrit Mohanty
#
# Depends on: PostgreSQL - data storage

"""
CRUD for the `configs` + `config_credentials` tables (sql/init.sql) - named
sets of scanner/ticket credentials, normalized so a new scanner/ticket
backend never needs a schema change (see sql/init.sql for why).

Every credential value (sonar_token, jira_api_token, webhook_secret, or
whatever a future backend needs) is encrypted via config.crypto before ever
reaching Postgres, and decrypted only by get_config() - list_configs()
never touches them, since it's for a picker list, not for use.

Called "configs", not "profiles" - see sql/init.sql for why.
"""

from config.connection import get_connection
from config.crypto import decrypt_token, encrypt_token
from config.ticket_destinations import (
    create_ticket_destination,
    get_ticket_destination,
    get_ticket_destination_by_id,
    list_ticket_destinations,
)

__all__ = [
    "create_config",
    "create_ticket_destination",
    "delete_config",
    "get_config",
    "get_connection",
    "get_ticket_destination",
    "list_configs",
    "list_ticket_destinations",
    "set_credential",
]


def create_config(
    name: str,
    scanner_type: str,
    scanner_mode: str,
    ticket_backend: str,
    trigger_mode: str,
    project_key: str,
    credentials: dict[str, str],
    sonar_plan: str | None = None,
    branches: str | None = None,
    ticket_destination_id: int | None = None,
) -> int:
    """
    Insert a new config row plus one config_credentials row per entry in
    `credentials` (each value encrypted before insert), in a single
    transaction - if the credentials insert fails, the configs row isn't
    left behind either. Returns the new config's id.

    project_key/sonar_plan/branches live on `configs` itself, not in
    `credentials` - none are secrets, and config_credentials'
    decrypt-on-read loop would break trying to Fernet-decrypt a plaintext
    value. All nullable at the DB level (configs created before each field
    existed predate it, and not every scanner_mode has a "plan" concept -
    sonar_plan is Cloud-only, branches is optional everywhere) - treat
    null/empty as "no restriction" wherever they're read, not an error.
    `branches` is a comma-separated list (e.g. "main,release/2.0"), one
    entry for a single-branch config, several for multi-branch Premium.

    `ticket_destination_id` references a shared ticket_destinations row
    instead of embedding Jira credentials here - leave it None for the
    legacy shape. get_config() resolves whichever shape a config uses.
    """
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO configs "
            "(name, scanner_type, scanner_mode, ticket_backend, trigger_mode, project_key, sonar_plan, branches, "
            "ticket_destination_id) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (
                name,
                scanner_type,
                scanner_mode,
                ticket_backend,
                trigger_mode,
                project_key,
                sonar_plan,
                branches,
                ticket_destination_id,
            ),
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

    If `ticket_destination_id` is set, Jira fields come from the
    referenced ticket_destinations row instead of this config's own
    config_credentials; if NULL (every pre-existing config), they're read
    straight out of config_credentials as always. Either way the returned
    "credentials" dict ends up the same flat shape, so callers
    (ticket/factory.py's build_ticket_client()) never need to know which
    path resolved it.
    """
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM configs WHERE name = %s", (name,))
        config_row = cur.fetchone()
        if config_row is None:
            return None

        cur.execute("SELECT key, value FROM config_credentials WHERE config_id = %s", (config_row["id"],))
        credential_rows = cur.fetchall()

    credentials = {row["key"]: decrypt_token(row["value"]) for row in credential_rows}

    destination_id = config_row.get("ticket_destination_id")
    if destination_id is not None:
        destination = get_ticket_destination_by_id(destination_id)
        if destination is not None:
            credentials["jira_project_key"] = destination["project_key"]
            credentials.update(destination["credentials"])

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


def set_credential(name: str, key: str, value: str) -> None:
    """
    Insert or update a single credential on an existing config, encrypting
    `value` first. Used by `gozu up` to persist a generated
    webhook_secret onto a config that predates one - not a general
    config-editing API (create_config() is still the only way to set
    everything else). Raises ValueError if no config named `name` exists.
    """
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT id FROM configs WHERE name = %s", (name,))
        row = cur.fetchone()
        if row is None:
            raise ValueError(f"No config named '{name}'")

        cur.execute(
            "INSERT INTO config_credentials (config_id, key, value) VALUES (%s, %s, %s) "
            "ON CONFLICT (config_id, key) DO UPDATE SET value = EXCLUDED.value",
            (row["id"], key, encrypt_token(value)),
        )
