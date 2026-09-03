"""Activity: fetch findings for a project via the ScannerClient interface."""

import subprocess

from temporalio import activity

from core.models import Finding
from scanner.client import build_scanner_client, get_scanner_client
from temporal.models.fetch_findings import FetchFindingsInput


def _get_git_branch() -> str | None:
    """
    SonarQube Community Build doesn't report per-issue branch info via its
    API (that's a Developer Edition+ feature), so this reads the branch
    that's actually checked out in this worker's own working directory -
    accurate for this project's local, single-checkout setup, but not a
    substitute for real branch-aware analysis.
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
    client = (
        build_scanner_client(input.scanner_type, input.scanner_mode, input.credentials)
        if input.credentials
        else get_scanner_client()
    )
    findings = client.fetch_findings(input.project_key)

    branch = _get_git_branch()
    for finding in findings:
        finding.branch = branch

    return findings
