# Copyright (c) 2026 Calfus Inc.
# Author: Wasiullah Rafeeq S

"""Input model for create_tickets_activity."""

from pydantic import BaseModel

from core.models import Finding


class CreateTicketsInput(BaseModel):
    findings: list[Finding]
    ticket_backend: str = "jira"
    credentials: dict[str, str] = {}
    # None -> create_tickets_activity falls back to its own BACKLOG_CAP
    # constant. Already the fully-resolved effective value by the time
    # this model is built (SonarToJiraInput.ticket_cap, itself resolved
    # once by the CLI from --ticket-cap vs the config's stored default) -
    # not re-resolved here.
    ticket_cap: int | None = None
