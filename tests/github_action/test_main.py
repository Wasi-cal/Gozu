# Copyright (c) 2026 Calfus Inc.
# Author: Prakrit Mohanty

from unittest.mock import MagicMock, patch

import pytest

from core.models import Finding, Severity
from github_action.main import main

MODULE = "github_action.main"
_FAKE_PNG = b"\x89PNG\r\n\x1a\nfake-png-bytes"


@pytest.fixture(autouse=True)
def action_env(monkeypatch):
    monkeypatch.setenv("SONAR_PROJECT_KEY", "proj")
    monkeypatch.setenv("COMMIT_TIMESTAMP", "2026-09-04T12:00:00+00:00")
    monkeypatch.setenv("SONAR_TOKEN", "sonar-token")


def make_finding(key: str = "ABC-1") -> Finding:
    return Finding(
        key=key,
        title="Some vulnerability (file.py:1)",
        severity=Severity.HIGH,
        component="proj:file.py",
        line=1,
        message="some message",
        finding_type="vulnerability",
        deep_link="https://sonarcloud.io/project/issues?id=proj",
        source_tool="sonarqube",
    )


def make_scanner_client(findings: list[Finding]) -> MagicMock:
    client = MagicMock()
    client.wait_for_latest_analysis.return_value = None
    client.fetch_findings.return_value = findings
    return client


def test_skips_finding_with_existing_ticket():
    finding = make_finding()
    scanner_client = make_scanner_client([finding])
    ticket_client = MagicMock()
    ticket_client.find_existing.return_value = "PROJ-1"

    with patch(f"{MODULE}.get_scanner_client", return_value=scanner_client), \
         patch(f"{MODULE}.get_ticket_client", return_value=ticket_client), \
         patch(f"{MODULE}.render_finding_snippet", return_value=_FAKE_PNG):
        main()

    ticket_client.create_ticket.assert_not_called()
    ticket_client.attach_screenshot.assert_not_called()


def test_creates_ticket_for_new_finding():
    finding = make_finding()
    scanner_client = make_scanner_client([finding])
    ticket_client = MagicMock()
    ticket_client.find_existing.return_value = None
    ticket_client.create_ticket.return_value = "PROJ-2"

    with patch(f"{MODULE}.get_scanner_client", return_value=scanner_client), \
         patch(f"{MODULE}.get_ticket_client", return_value=ticket_client), \
         patch(f"{MODULE}.render_finding_snippet", return_value=_FAKE_PNG):
        main()

    ticket_client.create_ticket.assert_called_once_with(finding)
    ticket_client.attach_screenshot.assert_called_once()
    ticket_client.add_comment.assert_called_once_with("PROJ-2", finding.message)


def test_propagates_real_create_ticket_error():
    finding = make_finding()
    scanner_client = make_scanner_client([finding])
    ticket_client = MagicMock()
    ticket_client.find_existing.return_value = None
    ticket_client.create_ticket.side_effect = RuntimeError("jira down")

    with patch(f"{MODULE}.get_scanner_client", return_value=scanner_client), \
         patch(f"{MODULE}.get_ticket_client", return_value=ticket_client), \
         patch(f"{MODULE}.render_finding_snippet", return_value=_FAKE_PNG), \
         pytest.raises(RuntimeError, match="jira down"):
        main()


def test_screenshot_failure_does_not_fail_run(caplog):
    finding = make_finding()
    scanner_client = make_scanner_client([finding])
    ticket_client = MagicMock()
    ticket_client.find_existing.return_value = None
    ticket_client.create_ticket.return_value = "PROJ-3"

    with patch(f"{MODULE}.get_scanner_client", return_value=scanner_client), \
         patch(f"{MODULE}.get_ticket_client", return_value=ticket_client), \
         patch(f"{MODULE}.render_finding_snippet", side_effect=RuntimeError("SonarQube returned 500")):
        main()  # a broken snippet render must not fail the run

    ticket_client.create_ticket.assert_called_once()
    ticket_client.attach_screenshot.assert_not_called()
    # Visible, not silent - item 9's requirement applies here too.
    assert finding.key in caplog.text
    assert "SonarQube returned 500" in caplog.text


def test_exits_nonzero_when_analysis_wait_times_out():
    scanner_client = MagicMock()
    scanner_client.wait_for_latest_analysis.side_effect = TimeoutError("timed out")
    ticket_client = MagicMock()

    with patch(f"{MODULE}.get_scanner_client", return_value=scanner_client), \
         patch(f"{MODULE}.get_ticket_client", return_value=ticket_client), \
         pytest.raises(SystemExit) as exc_info:
        main()

    assert exc_info.value.code == 1
    ticket_client.find_existing.assert_not_called()


def test_exits_nonzero_when_scanner_client_is_not_cloud(caplog):
    # e.g. SCANNER_TYPE misconfigured to self-hosted instead of
    # sonarqube-cloud - wait_for_latest_analysis only exists on
    # SonarQubeCloudClient, not the generic ScannerClient interface.
    scanner_client = MagicMock(spec=["fetch_findings", "requirements"])
    ticket_client = MagicMock()

    with patch(f"{MODULE}.get_scanner_client", return_value=scanner_client), \
         patch(f"{MODULE}.get_ticket_client", return_value=ticket_client), \
         pytest.raises(SystemExit) as exc_info:
        main()

    assert exc_info.value.code == 1
    assert "doesn't support wait_for_latest_analysis" in caplog.text
    ticket_client.find_existing.assert_not_called()
