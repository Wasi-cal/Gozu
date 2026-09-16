# Copyright (c) 2026 Calfus Inc.
# Author: Wasiullah Rafeeq S
# Editor: Prakrit Mohanty
#
# Depends on: Temporal (Temporal Technologies) - workflow orchestration
# Depends on: src/ticket/claims.py - ledger-based dedupe/claim of finding-to-ticket assignments
# Depends on: src/ticket/factory.py - builds/gets the ticket client used to create tickets
# Depends on: src/llm/factory.py - builds the optional LLM-enrichment client
# Depends on: src/llm/enrich.py - generates a finding's LLM explanation

"""Activity: create a ticket for each finding that doesn't already have one."""

from temporalio import activity

from core.models import CreatedTicket, Finding, Severity, TicketResult
from llm.enrich import enrich_finding
from llm.factory import build_llm_client
from temporal.models.create_tickets import CreateTicketsInput
from ticket import claims
from ticket.factory import build_ticket_client, get_ticket_client
from ticket.jira_client import (
    CUSTOM_FIELD_COMPONENT_NAME,
    CUSTOM_FIELD_LINE_NAME,
    CUSTOM_FIELD_SEVERITY_NAME,
)

# BLOCKER and CRITICAL both collapse to Severity.CRITICAL (see
# src/scanner/base.py's SONAR_SEVERITY_MAP), so this covers Blocker+Critical+High.
_LLM_ELIGIBLE_SEVERITIES = (Severity.CRITICAL, Severity.HIGH)


def _add_llm_explanation(finding: Finding, credentials: dict[str, str]) -> None:
    """Best-effort - a failure here must never block ticket creation.
    Called only once a finding is genuinely about to get a new ticket, so
    enrichment isn't wasted on already-ticketed findings every scan cycle."""
    if finding.severity not in _LLM_ELIGIBLE_SEVERITIES:
        return
    anthropic_key = credentials.get("anthropic_api_key")
    if not anthropic_key:
        return
    try:
        llm_client = build_llm_client(anthropic_key)
        finding.llm_explanation = enrich_finding(llm_client, finding, credentials.get("sonar_token", ""))
    except Exception as e:
        activity.logger.warning(f"LLM enrichment failed for finding {finding.key}: {e}")

# Per-run cap on new Jira issue-create calls, so a large one-off backlog
# doesn't try to create hundreds of tickets at once - anything past this
# goes into one rollup ticket instead and stays unclaimed for a future run.
# Fallback default only; input.ticket_cap (configs.ticket_cap or a
# --ticket-cap override) takes priority when set.
BACKLOG_CAP = 30


@activity.defn
async def create_tickets_activity(input: CreateTicketsInput) -> TicketResult:
    """Dedupe is two-layered: src/ticket/claims.py's Postgres ledger atomically
    claims a (destination, finding_key) pair before anything talks to Jira,
    closing the race between overlapping runs. client.find_existing()'s label
    search is a fallback for tickets that predate the ledger."""
    client = (
        build_ticket_client(input.ticket_backend, input.credentials) if input.credentials else get_ticket_client()
    )
    destination = client.destination_id()
    ticket_exists = getattr(client, "ticket_exists", None)

    # Once per activity, not per ticket. Best-effort like sprint assignment
    # and the rollup upsert below - a discovery outage must never block
    # ticket creation; create_ticket() treats an empty dict as "keep
    # Component/Line in Description".
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

    # One connection for the whole activity; each mutating call still
    # commits immediately, so concurrent runs see claims exactly as before.
    with claims.get_connection() as conn:
        for finding in input.findings:
            ledger_ticket = claims.get_ticket(conn, destination, finding.key)
            if ledger_ticket:
                # Trust the ledger unless the backend can cheaply confirm the
                # ticket still exists - one deleted outside gozu falls through
                # to the normal claim+create path below.
                if ticket_exists is None or ticket_exists(ledger_ticket):
                    skipped.append(finding.key)
                    continue
                activity.logger.warning(
                    f"Ticket {ledger_ticket} for finding {finding.key} no longer exists in "
                    f"{input.ticket_backend} (deleted outside gozu?) - clearing the stale claim"
                )
                claims.clear_stale(conn, destination, finding.key)

            if processed_new_count >= ticket_cap:
                # Not claimed, so a future run reconsiders it from scratch
                # once cap headroom frees up.
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

                _add_llm_explanation(finding, input.credentials)
                ticket_key = client.create_ticket(finding, custom_fields)
                claims.record_ticket(conn, destination, finding.key, ticket_key)
                created.append(CreatedTicket(finding_key=finding.key, ticket_key=ticket_key))
            except Exception:
                claims.release(conn, destination, finding.key)
                raise

    # Best-effort, same as sprint assignment in jira_client.py. Always
    # called even with an empty `deferred`, so an existing rollup ticket's
    # count can shrink back to 0, not just grow.
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
