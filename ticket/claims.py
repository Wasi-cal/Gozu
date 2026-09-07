# Copyright (c) 2026 Calfus Inc.
# Author: Wasiullah Rafeeq S
#
# Depends on: PostgreSQL - data storage

"""
Postgres-backed idempotency ledger for ticket creation - see
migrations/versions/0004_add_ticket_claims.py's `ticket_claims` table for why
this exists: it closes the check-then-act
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


def get_connection() -> psycopg.Connection[dict[str, Any]]:
    """
    One connection is meant to be opened once per activity invocation and
    passed into every claims call below - not one per call. A scan with N
    new findings used to open up to 3N short-lived connections (get_ticket
    + claim + record_ticket, each its own connect/auth/close) for what's
    really one activity's worth of work; a single reused connection turns
    that into a fixed, small number of round-trips.
    """
    return psycopg.Connection[dict[str, Any]].connect(
        host=os.environ["POSTGRES_HOST"],
        port=os.environ["POSTGRES_PORT"],
        user=os.environ["POSTGRES_USER"],
        password=os.environ["POSTGRES_PASSWORD"],
        dbname=os.environ["POSTGRES_DB"],
        row_factory=dict_row,
    )


def get_ticket(conn: psycopg.Connection[dict[str, Any]], destination: str, finding_key: str) -> str | None:
    """Already-recorded ticket for this (destination, finding), if any."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT ticket_key FROM ticket_claims WHERE destination = %s AND finding_key = %s",
            (destination, finding_key),
        )
        row = cur.fetchone()
        return row["ticket_key"] if row else None


def claim(conn: psycopg.Connection[dict[str, Any]], destination: str, finding_key: str) -> bool:
    """
    Atomically claim the right to create a ticket for this finding. Returns
    True if this call won the claim (proceed to create), False if someone
    else already holds it (skip - either they already created the ticket,
    or are creating one right now).
    """
    with conn.cursor() as cur:
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
        won = cur.fetchone() is not None
    # Committed immediately, not batched with the rest of this activity's
    # loop - a concurrent claim() on the same finding relies on seeing this
    # row (or not) right away. Held open across the whole loop instead,
    # Postgres would just block that other claim() until this entire
    # activity finished processing every finding, not just this one.
    conn.commit()
    return won


def record_ticket(conn: psycopg.Connection[dict[str, Any]], destination: str, finding_key: str, ticket_key: str) -> None:
    """Fill in the ticket_key for a claim this process won - marks it complete."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE ticket_claims SET ticket_key = %s WHERE destination = %s AND finding_key = %s",
            (ticket_key, destination, finding_key),
        )
    conn.commit()


def release(conn: psycopg.Connection[dict[str, Any]], destination: str, finding_key: str) -> None:
    """Give up a claim (e.g. after create_ticket() raised) so a retry can reclaim it immediately."""
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM ticket_claims WHERE destination = %s AND finding_key = %s AND ticket_key IS NULL",
            (destination, finding_key),
        )
    conn.commit()


def list_open(conn: psycopg.Connection[dict[str, Any]], destination: str) -> list[dict[str, str]]:
    """
    Every claim on `destination` with a real ticket_key that hasn't been
    marked closed yet - the candidates reconcile_resolved_findings_activity
    batch-checks against the scanner each run. Excludes in-progress claims
    (ticket_key IS NULL) - nothing to reconcile for a finding that doesn't
    have a ticket yet.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT finding_key, ticket_key FROM ticket_claims "
            "WHERE destination = %s AND status = 'open' AND ticket_key IS NOT NULL",
            (destination,),
        )
        return cur.fetchall()


def mark_closed(conn: psycopg.Connection[dict[str, Any]], destination: str, finding_key: str) -> None:
    """Record that this claim's ticket was auto-closed - list_open() won't surface it again."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE ticket_claims SET status = 'closed' WHERE destination = %s AND finding_key = %s",
            (destination, finding_key),
        )
    conn.commit()


def clear_stale(conn: psycopg.Connection[dict[str, Any]], destination: str, finding_key: str) -> None:
    """
    Remove a claim whose ticket_key was verified to no longer exist in the
    ticket backend (deleted directly there, outside gozu - see
    create_tickets_activity's ticket_exists() check). Distinct from
    release(): that one only ever clears an in-progress claim (ticket_key
    IS NULL); this one is specifically for a *completed* claim whose
    ticket has since vanished out from under the ledger, so it has no
    ticket_key guard - deleting the row lets the very next claim() for
    this finding succeed instead of permanently seeing a stale claim it
    can never win past.
    """
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM ticket_claims WHERE destination = %s AND finding_key = %s",
            (destination, finding_key),
        )
    conn.commit()
