# Copyright (c) 2026 Calfus Inc.
# Author: Wasiullah Rafeeq S

"""
Safety check for generating a new FERNET_KEY: refuses (or, with
force_new_key, confirms) when doing so would orphan Postgres rows already
encrypted under the old key. See config/crypto.py for why there's no
recovery path once that happens.
"""

from scripts.env_ports import DEFAULT_PORTS


class FernetKeySafetyError(RuntimeError):
    """Raised when generating a new FERNET_KEY would orphan existing encrypted rows."""


def fernet_key_safety_check(resolved: dict[str, str], force_new_key: bool) -> None:
    """
    Refuses to let a new FERNET_KEY be generated if Postgres is reachable
    right now AND already holds encrypted config rows. Returns normally
    when it's safe to generate a fresh key (Postgres unreachable - the
    normal case pre `docker compose up` - or reachable but empty).
    """
    postgres_user = resolved.get("POSTGRES_USER")
    postgres_password = resolved.get("POSTGRES_PASSWORD")
    postgres_db = resolved.get("POSTGRES_DB")
    if not (postgres_user and postgres_password and postgres_db):
        # First-ever bootstrap run - nothing could possibly exist to protect yet.
        return

    import psycopg

    try:
        conn = psycopg.connect(
            host=resolved.get("POSTGRES_HOST", "localhost"),
            port=resolved.get("POSTGRES_PORT", str(DEFAULT_PORTS["POSTGRES_PORT"])),
            user=postgres_user,
            password=postgres_password,
            dbname=postgres_db,
            connect_timeout=2,
        )
    except psycopg.OperationalError:
        # Postgres isn't up - the expected state before `docker compose up`. Safe.
        return

    try:
        with conn, conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM configs")
            row = cur.fetchone()
            count = row[0] if row else 0
    finally:
        conn.close()

    if count == 0:
        return

    if not force_new_key:
        raise FernetKeySafetyError(
            f"Postgres already contains {count} encrypted config row(s) and no FERNET_KEY was found "
            "in .env. Generating a new key would make those credentials permanently unreadable. "
            "If you still have the original FERNET_KEY, set it in .env and re-run. Otherwise, re-run "
            "with --force-new-key to generate a new one and accept the loss."
        )

    print(f"WARNING: Postgres already has {count} encrypted config row(s).")
    answer = input(
        f"Generating a new FERNET_KEY will make all {count} existing config(s) permanently unreadable. "
        "Type 'yes' to proceed: "
    )
    if answer.strip().lower() != "yes":
        raise FernetKeySafetyError("Aborted - answer was not 'yes'.")
