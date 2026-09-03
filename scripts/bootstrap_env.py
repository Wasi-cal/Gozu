#!/usr/bin/env python3
"""
Generates/merges .env: a random Postgres password, a fresh Fernet key, and
free host ports for Postgres/SonarQube/Temporal/Temporal's web UI/the
webhook receiver. Run once
per machine/checkout, before `docker compose up` - and safe to re-run any
time, since it only ever fills in fields that are still missing.

Field-by-field, not all-or-nothing: an existing .env is never overwritten
key-by-key - each of POSTGRES_USER/PASSWORD/DB/HOST, the four ports, and
FERNET_KEY is filled in independently if (and only if) it's missing. This
matters because an .env written before FERNET_KEY existed, or before a port
was added, would otherwise be silently skipped forever by an all-or-nothing
check, which is exactly what happened during Phase 1.

FERNET_KEY gets one extra safety check before generating a replacement (see
_fernet_key_safety_check): minting a new key while Postgres already holds
encrypted config rows makes them permanently undecryptable.

The core logic lives in importable functions (resolve_ports, bootstrap_env)
- codescan's `init` wizard calls these directly so it can show the user the
chosen ports and let them override before anything is written to disk,
rather than just running this script as a subprocess.
"""

import argparse
import os
import secrets
import socket
import sys
from datetime import UTC, datetime
from pathlib import Path

from cryptography.fernet import Fernet

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"

# Host-side default ports.
DEFAULT_PORTS: dict[str, int] = {
    "POSTGRES_PORT": 5432,
    "SONARQUBE_PORT": 9000,
    "TEMPORAL_PORT": 7233,
    "TEMPORAL_UI_PORT": 8233,
    "RECEIVER_PORT": 5000,
}


class FernetKeySafetyError(RuntimeError):
    """Raised when generating a new FERNET_KEY would orphan existing encrypted rows."""


def _port_is_free(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((host, port)) != 0


def find_free_port(preferred: int) -> int:
    """Return `preferred` if nothing's listening on it, else the next free port above it."""
    port = preferred
    while not _port_is_free(port):
        port += 1
    return port


def resolve_ports() -> dict[str, int]:
    """Probe each default port, auto-incrementing past whatever's already taken."""
    return {name: find_free_port(default) for name, default in DEFAULT_PORTS.items()}


def parse_env_file(path: Path) -> dict[str, str]:
    """Parse an existing .env's KEY=VALUE lines (surrounding quotes stripped) into a dict."""
    if not path.exists():
        return {}

    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def load_into_environ(path: Path = ENV_PATH) -> None:
    """
    Load .env's values into this process's environment - writing .env to
    disk (bootstrap_env()) doesn't, by itself, make POSTGRES_HOST etc
    visible to os.environ for the process that just wrote it. Never
    overwrites a value already set in the environment (a real exported
    env var, or an earlier call in the same process, wins over .env).
    """
    for key, value in parse_env_file(path).items():
        os.environ.setdefault(key, value)


def _fernet_key_safety_check(resolved: dict[str, str], force_new_key: bool) -> None:
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


def bootstrap_env(
    force_new_key: bool = False,
    port_overrides: dict[str, int] | None = None,
) -> dict[str, str]:
    """
    Fill in whatever fields are missing from .env (creating it if it
    doesn't exist yet). Returns the dict of fields that were newly written
    (empty if .env already had everything). `port_overrides` lets a caller
    (the init wizard) supply user-chosen ports instead of the auto-probed
    defaults, for ports not already present in .env.

    Raises FernetKeySafetyError instead of writing FERNET_KEY if that
    would orphan existing encrypted rows (see _fernet_key_safety_check).
    """
    existing = parse_env_file(ENV_PATH)
    ports = resolve_ports()
    if port_overrides:
        ports.update(port_overrides)

    new_fields: dict[str, str] = {}

    if "POSTGRES_USER" not in existing:
        new_fields["POSTGRES_USER"] = "sonarjira"
    if "POSTGRES_PASSWORD" not in existing:
        new_fields["POSTGRES_PASSWORD"] = secrets.token_urlsafe(32)
    if "POSTGRES_DB" not in existing:
        new_fields["POSTGRES_DB"] = "sonarjira"
    if "POSTGRES_HOST" not in existing:
        new_fields["POSTGRES_HOST"] = "localhost"

    for port_name in DEFAULT_PORTS:
        if port_name not in existing:
            new_fields[port_name] = str(ports[port_name])

    resolved = {**existing, **new_fields}

    if "TEMPORAL_HOST" not in existing:
        new_fields["TEMPORAL_HOST"] = f"localhost:{resolved['TEMPORAL_PORT']}"

    if "FERNET_KEY" not in existing:
        _fernet_key_safety_check(resolved, force_new_key)
        new_fields["FERNET_KEY"] = Fernet.generate_key().decode()

    if not new_fields:
        return {}

    try:
        with ENV_PATH.open("a") as f:
            if not existing:
                f.write("# Generated by scripts/bootstrap_env.py - do not commit this file.\n")
            timestamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%SZ")
            f.write(f"\n# --- Added by bootstrap_env.py on {timestamp} ---\n")
            for key, value in new_fields.items():
                f.write(f"{key}={value}\n")
    except OSError as e:
        raise OSError(f"Failed to write {ENV_PATH}: {e}") from e

    return new_fields


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--force-new-key",
        action="store_true",
        help="Allow generating a new FERNET_KEY even if Postgres already has encrypted config "
        "rows (those rows become permanently unreadable - you'll be asked to confirm).",
    )
    args = parser.parse_args()

    try:
        new_fields = bootstrap_env(force_new_key=args.force_new_key)
    except FernetKeySafetyError as e:
        print(f"Refusing to generate a new FERNET_KEY: {e}", file=sys.stderr)
        return 1
    except OSError as e:
        print(str(e), file=sys.stderr)
        return 1

    if not new_fields:
        print(f"{ENV_PATH} already has every required field - nothing to do.")
        return 0

    print(f"Updated {ENV_PATH}. New fields:")
    for key, value in new_fields.items():
        if key in ("POSTGRES_PASSWORD", "FERNET_KEY"):
            print(f"  {key}: generated (not shown)")
        else:
            print(f"  {key}={value}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
