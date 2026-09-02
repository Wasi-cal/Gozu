"""Activity: fetch vulnerabilities + security hotspots for a project via the Sonar interface."""

import subprocess

from temporalio import activity

from sonar.factory import get_sonar_client
from sonar.models import SonarIssue


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
    except Exception:
        return None


@activity.defn
async def fetch_vulnerabilities_activity(project_key: str) -> list[SonarIssue]:
    client = get_sonar_client()

    vulnerabilities = client.fetch_vulnerabilities(project_key)
    hotspots = client.fetch_hotspots(project_key)
    issues = vulnerabilities + hotspots

    branch = _get_git_branch()
    for issue in issues:
        issue.branch = branch

    return issues
