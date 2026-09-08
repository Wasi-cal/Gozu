# Copyright (c) 2026 Calfus Inc.
# Author: Wasiullah Rafeeq S

"""
Normalized domain vocabulary shared by every scanner adapter (scanner/client.py)
and every ticket adapter (ticket/client.py).

Neither side should ever see a tool-specific value (SonarQube's BLOCKER/MAJOR
severities, Jira's issue keys, etc) outside its own adapter - everything that
crosses the boundary between "fetch findings" and "create tickets" is expressed
in these types.
"""

from enum import Enum

from pydantic import BaseModel


class Severity(Enum):
    """
    Normalized severity vocabulary. Every scanner adapter translates its own
    tool-specific severity scale INTO this; every ticket adapter translates
    this OUT INTO its own tool-specific priority scale.
    """

    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    INFO = "INFO"


class Finding(BaseModel):
    """A single issue reported by a scanner, in tool-agnostic form."""

    key: str
    title: str
    severity: Severity
    component: str
    line: int | None
    message: str
    finding_type: str  # generic, e.g. "vulnerability" or "hotspot" - not tool-specific naming
    deep_link: str
    source_tool: str  # e.g. "sonarqube"
    branch: str | None = None  # scan branch, stamped by fetch_findings_activity
    # Rule-level "how to fix this" guidance (plain text, HTML stripped) -
    # generic per rule (e.g. "use tempfile.NamedTemporaryFile instead"),
    # never a fix tailored to this exact line/finding. None when the
    # scanner has no such guidance for this rule, or none at all (e.g.
    # a non-SonarQube scanner added later).
    how_to_fix: str | None = None


class CreatedTicket(BaseModel):
    finding_key: str
    ticket_key: str


class TicketResult(BaseModel):
    created: list[CreatedTicket] = []
    skipped: list[str] = []
    # Findings that were new (no existing ticket) but didn't get one this
    # run because create_tickets_activity's per-run backlog cap was
    # already reached - distinct from `skipped`, which means "already
    # ticketed", not "deferred". Rolled up into one shared ticket instead
    # of one each - see JiraClient.upsert_rollup_ticket().
    deferred: list[str] = []
    # The shared rollup ticket's key, set by create_tickets_activity
    # whenever `deferred` is non-empty and the ticket backend supports
    # upsert_rollup_ticket() - None otherwise (no deferred findings, or
    # the backend/attempt doesn't support it).
    rollup_ticket: str | None = None
    # Ticket keys reconcile_resolved_findings_activity auto-closed this
    # same run (temporal/workflows/scan_to_ticket.py) - attached onto this
    # same TicketResult rather than a separate model, since this is
    # already the one "what happened this run" result returned all the
    # way out to the CLI (cli/report.py's end-of-run summary).
    closed: list[str] = []
