# Copyright (c) 2026 Calfus Inc.
# Author: Wasiullah Rafeeq S
#
# Depends on: Temporal (Temporal Technologies) - workflow orchestration

"""Activity: auto-close tickets whose underlying finding SonarQube has since resolved."""

from temporalio import activity

from scanner.factory import build_scanner_client, get_scanner_client
from temporal.models.reconcile_resolved_findings import ReconcileResolvedFindingsInput
from ticket import claims
from ticket.factory import build_ticket_client, get_ticket_client

# SonarQube's resolution -> a readable phrase for the ticket comment. Keys
# match scanner/sonarqube_common.py's _RESOLVED_RESOLUTIONS exactly.
_RESOLUTION_LABELS = {
    "FIXED": "Fixed",
    "REMOVED": "Removed",
    "WONTFIX": "Won't Fix",
    "FALSE-POSITIVE": "False Positive",
}


@activity.defn
async def reconcile_resolved_findings_activity(input: ReconcileResolvedFindingsInput) -> list[str]:
    """
    Runs every scan cycle alongside ticket creation, not on a separate
    trigger - always on for every config, no opt-out field. For every
    still-open claim on this destination, batch-checks the scanner for
    whether the underlying finding is now resolved; anything that is gets
    its ticket transitioned to a "done"-category status, a comment
    explaining why, and the claim marked closed so it's never re-checked.
    Returns the ticket keys actually closed this run.

    A ticket backend without transition support (getattr returns None) or
    a ticket with no "done"-category transition available both log and
    skip rather than raising - this is a bonus reconciliation step, never
    allowed to fail the workflow it runs alongside (see
    temporal/workflows/scan_to_ticket.py's try/except around this call).

    Confirmed live: a single claim whose ticket_key no longer exists in
    Jira (deleted directly, outside gozu) makes transition_to_done() raise
    a real 404 - without its own try/except, that exception propagated out
    of this whole activity and silently skipped reconciling every OTHER
    claim in the same run too, not just the broken one. Each claim is now
    handled independently: one bad claim logs a warning and the loop moves
    on, same "one bad item can't sink the batch" rule this codebase already
    applies to sprint assignment (ticket/jira_sprint.py) and the rollup
    ticket (create_tickets_activity).
    """
    scanner_client = (
        build_scanner_client(input.scanner_type, input.scanner_mode, input.credentials)
        if input.credentials
        else get_scanner_client()
    )
    ticket_client = (
        build_ticket_client(input.ticket_backend, input.credentials) if input.credentials else get_ticket_client()
    )
    destination = ticket_client.destination_id()

    transition_to_done = getattr(ticket_client, "transition_to_done", None)
    if transition_to_done is None:
        activity.logger.warning(f"{input.ticket_backend} doesn't support transitions - skipping auto-close")
        return []

    add_comment = getattr(ticket_client, "add_comment", None)
    closed: list[str] = []

    with claims.get_connection() as conn:
        open_claims = claims.list_open(conn, destination)
        if not open_claims:
            return []

        resolutions = scanner_client.fetch_resolutions([row["finding_key"] for row in open_claims])
        if not resolutions:
            return []

        for row in open_claims:
            finding_key = row["finding_key"]
            resolution = resolutions.get(finding_key)
            if resolution is None:
                continue

            ticket_key = row["ticket_key"]
            try:
                if not transition_to_done(ticket_key):
                    activity.logger.warning(
                        f"{ticket_key} has no 'done'-category transition available right now - skipping auto-close"
                    )
                    continue

                if add_comment:
                    label = _RESOLUTION_LABELS.get(resolution, resolution)
                    add_comment(ticket_key, f"Closed automatically - SonarQube marked this {label}")

                claims.mark_closed(conn, destination, finding_key)
                closed.append(ticket_key)
            except Exception as e:
                activity.logger.warning(f"Auto-close failed for {ticket_key} (finding {finding_key}), leaving it open: {e}")

    return closed
