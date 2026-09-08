# Copyright (c) 2026 Calfus Inc.
# Author: Wasiullah Rafeeq S
#
# Depends on: Temporal (Temporal Technologies) - workflow orchestration
# Depends on: ticket/claims.py - ledger-based dedupe/claim of finding-to-ticket assignments
# Depends on: ticket/factory.py - builds/gets the ticket client used to create tickets

"""Activity: create a ticket for each finding that doesn't already have one."""

from temporalio import activity

from core.models import CreatedTicket, TicketResult
from temporal.models.create_tickets import CreateTicketsInput
from ticket import claims
from ticket.factory import build_ticket_client, get_ticket_client
from ticket.jira_client import CUSTOM_FIELD_COMPONENT_NAME, CUSTOM_FIELD_LINE_NAME, CUSTOM_FIELD_SEVERITY_NAME

# Per-run cap on genuinely new tickets (Jira issue-create calls) - a named
# constant, not a magic number, since a large one-off backlog (e.g. this
# project's very first scan) would otherwise try to create hundreds of
# tickets in one activity. Anything past this goes into one rollup ticket
# instead (see JiraClient.upsert_rollup_ticket()) and is deliberately left
# unclaimed, so a future run picks it back up as real cap headroom frees up.
#
# The FALLBACK default, not the only option anymore - input.ticket_cap
# (configs.ticket_cap, migrations/versions/0008_add_ticket_cap.py, or a
# --ticket-cap one-off override - see cli/scan_runner/__init__.py, which
# resolves the two before this activity ever sees the result) overrides
# this per-run when set; this constant only governs when neither was.
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
    ticket_exists = getattr(client, "ticket_exists", None)

    # Once per activity execution, not once per ticket - the same Jira
    # instance backs every ticket this run creates. Missing entirely on a
    # backend without discover_custom_fields() (or if this Jira instance
    # has neither optional field configured) -> empty dict, which
    # create_ticket() already treats as "keep Component/Line in
    # Description", identical to today's behavior. Same bonus/best-effort
    # treatment as sprint assignment and the rollup-ticket upsert below -
    # confirmed live that an unreachable/misconfigured Jira instance makes
    # this a real RuntimeError, which (unwrapped) failed this entire
    # activity before a single finding was even looked at, regardless of
    # ticket_cap - a field-discovery outage must never be able to block
    # ticket creation itself.
    custom_fields = {}
    discover_custom_fields = getattr(client, "discover_custom_fields", None)
    if discover_custom_fields:
        try:
            custom_fields = discover_custom_fields(
                [CUSTOM_FIELD_COMPONENT_NAME, CUSTOM_FIELD_LINE_NAME, CUSTOM_FIELD_SEVERITY_NAME]
            )
        except Exception as e:
            activity.logger.warning(f"Custom field discovery failed, falling back to Description for all fields: {e}")

    ticket_cap = input.ticket_cap if input.ticket_cap is not None else BACKLOG_CAP

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
                # The ledger's ticket_key is trusted blindly UNLESS the
                # backend can cheaply confirm it's still real - a ticket
                # deleted directly in Jira (outside gozu entirely) leaves
                # this row pointing at nothing, permanently "skipping" a
                # finding that in fact has no ticket at all. Verified
                # False -> the claim is cleared and this finding falls
                # through to the normal claim+create path below, same run.
                if ticket_exists is None or ticket_exists(ledger_ticket):
                    skipped.append(finding.key)
                    continue
                activity.logger.warning(
                    f"Ticket {ledger_ticket} for finding {finding.key} no longer exists in "
                    f"{input.ticket_backend} (deleted outside gozu?) - clearing the stale claim"
                )
                claims.clear_stale(conn, destination, finding.key)

            if processed_new_count >= ticket_cap:
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

                ticket_key = client.create_ticket(finding, custom_fields)
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
    rollup_ticket = None
    upsert_rollup_ticket = getattr(client, "upsert_rollup_ticket", None)
    if upsert_rollup_ticket:
        try:
            deferred_findings = [finding for finding in input.findings if finding.key in deferred]
            rollup_ticket = upsert_rollup_ticket(deferred_findings)
            if rollup_ticket:
                activity.logger.info(f"Backlog rollup ticket {rollup_ticket}: {len(deferred)} deferred finding(s)")
        except Exception as e:
            activity.logger.warning(f"Backlog rollup ticket upsert failed: {e}")
            rollup_ticket = None

    return TicketResult(created=created, skipped=skipped, deferred=deferred, rollup_ticket=rollup_ticket)
