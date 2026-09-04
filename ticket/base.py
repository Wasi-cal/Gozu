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
    def create_ticket(self, finding: Finding) -> str:
        """Create a ticket for a finding, return the new ticket's key."""
        raise NotImplementedError

    # attach_screenshot()/add_comment() are deliberately NOT part of this
    # contract - they're optional, backend-specific bonus capabilities
    # (see jira_client.py). Callers use getattr(client, name, None) to
    # detect support rather than calling them directly (see
    # temporal/activities/capture_and_attach_screenshot.py).
