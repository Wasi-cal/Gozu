# Copyright (c) 2026 Calfus Inc.
# Author: Wasiullah Rafeeq S

"""
Generic ticketing interface every backend (Jira, and whatever's added
later - Linear, GitHub Issues) implements. Adding a new destination means
writing a new TicketClient subclass and registering it in
ticket/factory.py - nothing in core/models.py, scanner/, or the Temporal
workflow/activities/receiver needs to change.
"""

from abc import ABC, abstractmethod

from core.models import Finding, Severity

# Normalized Severity -> Jira priority name. Backend-specific mappings like
# this live next to the client that uses them (see jira_client.py) once
# there's more than one backend; kept here for now since it's the only one.
SEVERITY_TO_PRIORITY = {
    Severity.CRITICAL: "Highest",
    Severity.HIGH: "High",
    Severity.MEDIUM: "Medium",
    Severity.LOW: "Low",
    Severity.INFO: "Low",
}
DEFAULT_PRIORITY = "Medium"

SUMMARY_MAX_LENGTH = 255


class TicketClient(ABC):
    @abstractmethod
    def destination_id(self) -> str:
        """
        A stable string identifying where this client creates tickets (e.g.
        "jira:{base_url}:{project_key}") - scopes ticket/claims.py's
        idempotency ledger, so two genuinely different destinations tracking
        the same finding key don't collide with each other.
        """
        raise NotImplementedError

    @abstractmethod
    def find_existing(self, finding_key: str) -> str | None:
        """Return the key of an existing ticket for this finding, if any (dedupe)."""
        raise NotImplementedError

    @abstractmethod
    def create_ticket(self, finding: Finding, custom_fields: dict[str, str] | None = None) -> str:
        """
        Create a ticket for a finding, return the new ticket's key.

        `custom_fields` is optional and backend-specific (Jira: whichever
        of ticket/jira_client.py's CUSTOM_FIELD_COMPONENT_NAME/
        CUSTOM_FIELD_LINE_NAME were found by discover_custom_fields() -
        see create_tickets_activity, which calls that once per activity
        run and passes the result through here for every ticket). Kept as
        a plain optional parameter on this required method (default None,
        a backend free to ignore it) rather than its own getattr-detected
        bonus method, since it's part of ticket creation itself, not a
        separate step - unlike attach_screenshot()/add_comment()/
        transition_to_done()/upsert_rollup_ticket()/ticket_exists()/
        discover_custom_fields(), which ARE deliberately NOT part of this
        contract: optional, backend-specific bonus capabilities (see
        jira_client.py). Callers use getattr(client, name, None) to
        detect support for those rather than calling them directly (see
        temporal/activities/capture_and_attach_screenshot.py,
        temporal/activities/reconcile_resolved_findings.py, and
        temporal/activities/create_tickets.py's backlog rollup,
        stale-ticket-claim recovery, and custom-field discovery).
        """
        raise NotImplementedError
