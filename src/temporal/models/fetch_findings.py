# Copyright (c) 2026 Calfus Inc.
# Author: Wasiullah Rafeeq S
# Editor: Prakrit Mohanty

"""Input model for fetch_findings_activity."""

from pydantic import BaseModel

from core.models import Finding


class FetchFindingsInput(BaseModel):
    project_key: str
    scanner_type: str = "sonarqube"
    scanner_mode: str = "local"
    credentials: dict[str, str] = {}
    branch: str | None = None

    # Purely for labeling findings (finding.branch, shown on the Jira
    # ticket) - see SonarToJiraInput.display_branch's docstring for why
    # this is a separate value from `branch` above, not the same one.
    display_branch: str | None = None

    # Findings already computed host-side (src/cli/scan_runner/semgrep_exec.py)
    # before this activity ever runs - for a scanner with no server to
    # query remotely (local Semgrep), the worker has no access to the
    # host checkout the scan needs, so there's nothing for it to fetch.
    # None (every other scanner) means "fetch normally"; see
    # temporal/activities/fetch_findings.py.
    pre_fetched_findings: list[Finding] | None = None
