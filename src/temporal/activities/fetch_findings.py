# Copyright (c) 2026 Calfus Inc.
# Author: Wasiullah Rafeeq S
# Editor: Prakrit Mohanty
#
# Depends on: Temporal (Temporal Technologies) - workflow orchestration
# Depends on: src/scanner/factory.py - builds/gets the scanner client used to fetch findings

"""Activity: fetch findings for a project via the ScannerClient interface."""

import subprocess

from temporalio import activity

from core.models import Finding
from scanner.factory import build_scanner_client, get_scanner_client
from temporal.models.fetch_findings import FetchFindingsInput


def _get_git_branch() -> str | None:
    """
    Fallback only, used when nothing supplied FetchFindingsInput.branch
    (the webhook receiver's path - it has no host git checkout to inspect
    either). Reads the branch checked out in the *worker container's own*
    working directory, which structurally can't reflect anything real:
    the image never contains a .git directory (excluded via
    .dockerignore) - this exists to preserve prior behavior, not because
    it's expected to resolve to anything but None in practice.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip() or None
    except Exception:  # noqa: BLE001 - branch is best-effort metadata, never worth failing the fetch over
        return None


@activity.defn
async def fetch_findings_activity(input: FetchFindingsInput) -> list[Finding]:
    branch = input.branch if input.branch is not None else _get_git_branch()

    # Deliberately NOT always `branch` - that value is also what scoped the
    # fetch_findings() query below, which stays gated to configs SonarQube
    # actually supports branch-scoping for (see SonarToJiraInput.branch's
    # docstring: None for local/Community and Cloud Free). Ticket labeling
    # has no such restriction - display_branch is the host's real git
    # branch regardless of scanner_mode/sonar_plan (see
    # src/cli/scan_runner/__init__.py), so a local/Free config's tickets show
    # the actual branch scanned instead of "unknown" just because
    # SonarQube itself couldn't be told to scope by it.
    label = input.display_branch if input.display_branch is not None else branch

    if input.pre_fetched_findings is not None:
        # A scanner with no server to query remotely (local Semgrep) runs
        # host-side, before this activity ever executes - see
        # src/cli/scan_runner/semgrep_exec.py. Nothing to fetch here; just
        # stamp the branch label, same as the normal path below does.
        for finding in input.pre_fetched_findings:
            finding.branch = label
        return input.pre_fetched_findings

    client = (
        build_scanner_client(input.scanner_type, input.scanner_mode, input.credentials)
        if input.credentials
        else get_scanner_client()
    )
    findings = client.fetch_findings(input.project_key, branch=branch)
    for finding in findings:
        finding.branch = label

    return findings
