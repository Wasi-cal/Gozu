# Copyright (c) 2026 Calfus Inc.
# Author: Wasiullah Rafeeq S
# Editor: Prakrit Mohanty

"""Input model for ScanToTicketWorkflow."""

from pydantic import BaseModel

from core.models import Finding


class SonarToJiraInput(BaseModel):
    # str for every SonarQube config. None for Trivy, whose scan target is
    # a host filesystem path (pre_fetched_findings below), not a project_key.
    project_key: str | None = None
    task_id: str | None = None

    # A specific config's scanner/ticket backend + credentials, passed
    # straight through from config_store.get_config(). All default to the
    # legacy single-global-config values so the webhook receiver (which
    # never sets these) falls back to its env-var-based client builders.
    scanner_type: str = "sonarqube"
    scanner_mode: str = "local"
    ticket_backend: str = "jira"
    credentials: dict[str, str] = {}

    # The git branch actually scanned, computed on the host (the worker
    # container has no .git). Also scopes fetch_findings_activity's own
    # query, so it's left None for configs SonarQube can't branch-scope
    # (self-hosted Community Build, Cloud Free).
    branch: str | None = None

    # The git branch to LABEL tickets with - always the host's real branch,
    # unlike `branch` above, since SonarQube doesn't need to agree with it
    # for it to be useful on a ticket.
    display_branch: str | None = None

    # The effective per-run new-ticket cap, already resolved client-side
    # from --ticket-cap or the config's stored ticket_cap. None means
    # neither was set; create_tickets_activity falls back to BACKLOG_CAP.
    ticket_cap: int | None = None

    # Findings already fetched HOST-SIDE (src/cli/scan_runner/), before this
    # workflow was even triggered - set only for scanners whose scan is a
    # local-filesystem operation (Trivy `fs` mode) the Temporal worker
    # container can't perform itself (no volume mount of the scanned path).
    # When set, ScanToTicketWorkflow.run() skips fetch_findings_activity
    # entirely. Deliberate trade-off: a transient trivy fs failure isn't
    # covered by Temporal's activity retry, the same way a transient
    # sonar-scanner failure (also host-side) already isn't.
    pre_fetched_findings: list[Finding] | None = None
