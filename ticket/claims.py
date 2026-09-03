"""
Postgres-backed idempotency ledger for ticket creation - see sql/init.sql's
`ticket_claims` table for why this exists: it closes the check-then-act
race in create_tickets_activity's old find_existing()-then-create_ticket()
pattern, where two overlapping runs (concurrent multi-branch fan-out, an
activity retry after a partial failure, or two configs pointed at the same
project) could each see "no ticket yet" and each create one.

Not encrypted (config/crypto.py) - nothing here is a secret, just a
destination+finding_key -> ticket_key mapping.
"""

import os
from typing import Any

import psycopg
from psycopg.rows import dict_row

# Long enough for a real create_ticket() call to finish, short enough that
# a crashed worker doesn't block a finding for long - see claim()'s
# self-healing DELETE below.
_STALE_CLAIM_SECONDS = 300


def _get_connection() -> psycopg.Connection[dict[str, Any]]:
    return psycopg.Connection[dict[str, Any]].connect(
        host=os.environ["POSTGRES_HOST"],
        port=os.environ["POSTGRES_PORT"],
        user=os.environ["POSTGRES_USER"],
        password=os.environ["POSTGRES_PASSWORD"],
        dbname=os.environ["POSTGRES_DB"],
        row_factory=dict_row,
    )


def get_ticket(destination: str, finding_key: str) -> str | None:
    """Already-recorded ticket for this (destination, finding), if any."""
    with _get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT ticket_key FROM ticket_claims WHERE destination = %s AND finding_key = %s",
            (destination, finding_key),
        )
        row = cur.fetchone()
        return row["ticket_key"] if row else None


def claim(destination: str, finding_key: str) -> bool:
    """
    Atomically claim the right to create a ticket for this finding. Returns
    True if this call won the claim (proceed to create), False if someone
    else already holds it (skip - either they already created the ticket,
    or are creating one right now).
    """
    with _get_connection() as conn, conn.cursor() as cur:
        # Self-heal a crashed attempt: a claim with no ticket_key recorded
        # after _STALE_CLAIM_SECONDS is assumed abandoned, so it doesn't
        # permanently block this finding.
        cur.execute(
            "DELETE FROM ticket_claims WHERE destination = %s AND finding_key = %s "
            "AND ticket_key IS NULL AND created_at < now() - make_interval(secs => %s)",
            (destination, finding_key, _STALE_CLAIM_SECONDS),
        )
        cur.execute(
            "INSERT INTO ticket_claims (destination, finding_key) VALUES (%s, %s) "
            "ON CONFLICT (destination, finding_key) DO NOTHING RETURNING 1",
            (destination, finding_key),
        )
        return cur.fetchone() is not None


def record_ticket(destination: str, finding_key: str, ticket_key: str) -> None:
    """Fill in the ticket_key for a claim this process won - marks it complete."""
    with _get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE ticket_claims SET ticket_key = %s WHERE destination = %s AND finding_key = %s",
            (ticket_key, destination, finding_key),
        )


def release(destination: str, finding_key: str) -> None:
    """Give up a claim (e.g. after create_ticket() raised) so a retry can reclaim it immediately."""
    with _get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "DELETE FROM ticket_claims WHERE destination = %s AND finding_key = %s AND ticket_key IS NULL",
            (destination, finding_key),
        )
