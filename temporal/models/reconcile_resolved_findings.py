# Copyright (c) 2026 Calfus Inc.
# Author: Wasiullah Rafeeq S

"""Input model for reconcile_resolved_findings_activity."""

from pydantic import BaseModel


class ReconcileResolvedFindingsInput(BaseModel):
    # No project_key/branch here (unlike FetchFindingsInput/CreateTicketsInput):
    # ticket_claims is already scoped per-destination (ticket backend +
    # project, see TicketClient.destination_id()), and the scanner side
    # looks up specific finding keys directly (issues/search's `issues`
    # param), not a project's full finding list - see
    # scanner/sonarqube_common.py's fetch_sonarqube_resolutions().
    scanner_type: str = "sonarqube"
    scanner_mode: str = "local"
    ticket_backend: str = "jira"
    credentials: dict[str, str] = {}
