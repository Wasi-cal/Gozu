# Copyright (c) 2026 Calfus Inc.
# Author: Prakrit Mohanty

from unittest.mock import MagicMock, patch

from core.models import Finding, Severity
from temporal.activities.fetch_findings import fetch_findings_activity
from temporal.models.fetch_findings import FetchFindingsInput

MODULE = "temporal.activities.fetch_findings"


def make_finding(key: str, source_tool: str = "semgrep") -> Finding:
    return Finding(
        key=key,
        title="Some vulnerability",
        severity=Severity.HIGH,
        component="app.py",
        line=1,
        message="some message",
        finding_type="vulnerability",
        deep_link="https://semgrep.dev",
        source_tool=source_tool,
    )


async def test_pre_fetched_findings_skips_scanner_client_entirely():
    """Local Semgrep's findings are already computed host-side - no client should ever be built for this path."""
    findings = [make_finding("k1"), make_finding("k2")]

    with patch(f"{MODULE}.build_scanner_client") as mock_build, patch(f"{MODULE}.get_scanner_client") as mock_get:
        result = await fetch_findings_activity(
            FetchFindingsInput(project_key="/some/path", pre_fetched_findings=findings, display_branch="main")
        )

    mock_build.assert_not_called()
    mock_get.assert_not_called()
    assert result == findings


async def test_pre_fetched_findings_still_get_branch_label_stamped():
    findings = [make_finding("k1")]
    assert findings[0].branch is None

    result = await fetch_findings_activity(
        FetchFindingsInput(project_key="/some/path", pre_fetched_findings=findings, display_branch="feature/x")
    )

    assert result[0].branch == "feature/x"


async def test_none_pre_fetched_findings_falls_back_to_normal_client_fetch():
    findings = [make_finding("k1", source_tool="sonarqube")]
    client = MagicMock()
    client.fetch_findings.return_value = findings

    with patch(f"{MODULE}.get_scanner_client", return_value=client):
        result = await fetch_findings_activity(FetchFindingsInput(project_key="proj", branch="main"))

    client.fetch_findings.assert_called_once_with("proj", branch="main")
    assert result == findings
