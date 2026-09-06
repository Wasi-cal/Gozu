# Copyright (c) 2026 Calfus Inc.
# Author: Wasiullah Rafeeq S
#
# Depends on: Temporal (Temporal Technologies) - workflow orchestration

"""Activity: create a ticket for each finding that doesn't already have one."""

from temporalio import activity

from core.models import CreatedTicket, TicketResult
from temporal.models.create_tickets import CreateTicketsInput
from ticket import claims
from ticket.factory import build_ticket_client, get_ticket_client

# Per-run cap on genuinely new tickets (Jira issue-create calls) - a named
# constant, not a magic number, since a large one-off backlog (e.g. this
# project's very first scan) would otherwise try to create hundreds of
# tickets in one activity. Anything past this goes into one rollup ticket
# instead (see JiraClient.upsert_rollup_ticket()) and is deliberately left
# unclaimed, so a future run picks it back up as real cap headroom frees up.
BACKLOG_CAP = 30


@activity.defn
async def create_tickets_activity(input: CreateTicketsInput) -> TicketResult:
    """
    Dedupe is two-layered: ticket/claims.py's Postgres ledger atomically
    claims a (destination, finding_key) pair before anything talks to Jira,
    closing the race where two overlapping runs (concurrent multi-branch
    fan-out, an activity retry, two configs on the same project) could both
    pass a plain "does a ticket exist yet" check before either creates one.
    client.find_existing()'s label search is still used as a fallback for
    tickets that predate the ledger or were created some other way.
    """
    client = (
        build_ticket_client(input.ticket_backend, input.credentials) if input.credentials else get_ticket_client()
    )
    destination = client.destination_id()

    created = []
    skipped = []
    deferred = []
    processed_new_count = 0

    # One connection for the whole activity, not one per claims call per
    # finding - see claims.get_connection(). Each mutating call still
    # commits immediately (claim/record_ticket/release), so concurrent
    # runs see each other's claims exactly as before; only the
    # connect/auth/close overhead is no longer paid per finding.
    with claims.get_connection() as conn:
        for finding in input.findings:
            ledger_ticket = claims.get_ticket(conn, destination, finding.key)
            if ledger_ticket:
                skipped.append(finding.key)
                continue

            if processed_new_count >= BACKLOG_CAP:
                # Not claimed - this finding is genuinely untouched, so a
                # future run (once earlier findings free up cap headroom,
                # or just because this run's cap resets) reconsiders it
                # from scratch instead of it being stuck "handled" with no
                # real ticket to show for it.
                deferred.append(finding.key)
                continue

            if not claims.claim(conn, destination, finding.key):
                # Another concurrent run holds this claim right now.
                skipped.append(finding.key)
                continue

            processed_new_count += 1
            try:
                existing_ticket = client.find_existing(finding.key)
                if existing_ticket:
                    claims.record_ticket(conn, destination, finding.key, existing_ticket)
                    skipped.append(finding.key)
                    continue

                ticket_key = client.create_ticket(finding)
                claims.record_ticket(conn, destination, finding.key, ticket_key)
                created.append(CreatedTicket(finding_key=finding.key, ticket_key=ticket_key))
            except Exception:
                claims.release(conn, destination, finding.key)
                raise

    # Bonus, best-effort step, same treatment as sprint assignment in
    # jira_client.py - a rollup-ticket failure must never fail the whole
    # activity when every per-finding ticket above already succeeded.
    # Always called, even with an empty `deferred`: that's what lets an
    # existing rollup ticket's count shrink back to 0 as the backlog gets
    # worked through, not just grow.
    upsert_rollup_ticket = getattr(client, "upsert_rollup_ticket", None)
    if upsert_rollup_ticket:
        try:
            deferred_findings = [finding for finding in input.findings if finding.key in deferred]
            rollup_ticket = upsert_rollup_ticket(deferred_findings)
            if rollup_ticket:
                activity.logger.info(f"Backlog rollup ticket {rollup_ticket}: {len(deferred)} deferred finding(s)")
        except Exception as e:
            activity.logger.warning(f"Backlog rollup ticket upsert failed: {e}")

    return TicketResult(created=created, skipped=skipped, deferred=deferred)
