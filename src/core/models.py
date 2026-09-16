# Copyright (c) 2026 Calfus Inc.
# Author: Wasiullah Rafeeq S
# Editor: Prakrit Mohanty

"""
Normalized domain vocabulary shared by every scanner adapter (src/scanner/client.py)
and every ticket adapter (src/ticket/client.py).

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
    # generic per rule, never tailored to this exact line. None when the
    # scanner has no such guidance.
    how_to_fix: str | None = None
    # The scanner's own rule identifier - src/llm/enrich.py needs it to
    # recognize hardcoded-credential rules and withhold the code snippet.
    rule_key: str | None = None
    # LLM-generated explanation + suggested fix for a new Blocker/Critical/
    # High ticket only. None means not generated - falls back to finding.message.
    llm_explanation: str | None = None
    # Package-level metadata (src/scanner/trivy_client.py) - a package-level
    # finding has no file+line. All three set together or not at all;
    # fixed_version is None specifically when no fix is available yet, a
    # distinct state from "unknown" (see ticket/jira_client.py, which
    # branches on `if finding.package_name:`, never on `source_tool`).
    package_name: str | None = None
    installed_version: str | None = None
    fixed_version: str | None = None


class CreatedTicket(BaseModel):
    finding_key: str
    ticket_key: str


class TicketResult(BaseModel):
    created: list[CreatedTicket] = []
    skipped: list[str] = []
    # New findings that didn't get a ticket this run (backlog cap reached),
    # distinct from `skipped` ("already ticketed"). Rolled up into one
    # shared ticket - see JiraClient.upsert_rollup_ticket().
    deferred: list[str] = []
    # The shared rollup ticket's key, set whenever `deferred` is non-empty
    # and the backend supports upsert_rollup_ticket().
    rollup_ticket: str | None = None
    # Ticket keys reconcile_resolved_findings_activity auto-closed this run -
    # attached here since this is the one "what happened" result returned
    # out to the CLI.
    closed: list[str] = []
