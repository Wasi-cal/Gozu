# Copyright (c) 2026 Calfus Inc.
# Author: Wasiullah Rafeeq S

from unittest.mock import AsyncMock, MagicMock, patch

from core.models import Finding, Severity, TicketResult

MODULE = "cli.scan_runner"


def make_trivy_config() -> dict:
    return {
        "name": "trivy-config",
        "scanner_type": "trivy",
        "scanner_mode": "local",
        "ticket_backend": "jira",
        "credentials": {},
        "project_key": None,
        "sonar_plan": None,
        "branches": None,
        "ticket_cap": None,
    }


def make_finding(key: str) -> Finding:
    return Finding(
        key=key,
        title="CVE-2018-1000656: python-flask DoS (flask)",
        severity=Severity.HIGH,
        component="requirements.txt",
        line=None,
        message="DoS via crafted JSON",
        finding_type="vulnerability",
        deep_link="https://avd.aquasec.com/nvd/cve-2018-1000656",
        source_tool="trivy",
        package_name="flask",
        installed_version="0.12",
        fixed_version="0.12.3",
    )


def test_run_scan_cycle_dispatches_to_trivy_for_trivy_scanner_type():
    """
    Confirms the scanner_type == "trivy" branch in run_scan_cycle() picks
    the Trivy path - no sonar-scanner/Java prerequisite check, no
    SonarQube-specific host_url/token access, no ceTaskId polling.
    """
    from cli.scan_runner import run_scan_cycle

    config = make_trivy_config()
    findings = [make_finding("CVE-2018-1000656|flask|requirements.txt")]
    fake_ticket_result = TicketResult(created=[], skipped=[], deferred=[], closed=[])

    with (
        patch(f"{MODULE}.ensure_trivy") as mock_ensure_trivy,
        patch(f"{MODULE}.ensure_java") as mock_ensure_java,
        patch(f"{MODULE}.ensure_sonar_scanner") as mock_ensure_sonar_scanner,
        patch(f"{MODULE}.TrivyClient") as mock_trivy_client_cls,
        patch(f"{MODULE}.trigger_workflow", new_callable=AsyncMock) as mock_trigger_workflow,
        patch(f"{MODULE}.detect_git_branch", return_value="main"),
    ):
        mock_trivy_client_cls.return_value.fetch_findings.return_value = findings
        mock_trigger_workflow.return_value = fake_ticket_result

        ce_task_id, scanned_branches, ticket_result = run_scan_cycle(config, "/some/repo")

    mock_ensure_trivy.assert_called_once()
    mock_ensure_java.assert_not_called()
    mock_ensure_sonar_scanner.assert_not_called()
    mock_trivy_client_cls.return_value.fetch_findings.assert_called_once_with("/some/repo")

    # ce_task_id is a descriptive placeholder, not a real SonarQube task id.
    assert "trivy" in ce_task_id
    assert scanned_branches == []
    assert ticket_result is fake_ticket_result

    # trigger_workflow() got the pre-fetched findings, no ce_task_id, no branch.
    call_kwargs = mock_trigger_workflow.call_args.kwargs
    assert call_kwargs["pre_fetched_findings"] == findings
    assert call_kwargs["ce_task_id"] is None
    assert call_kwargs["branch"] is None
    assert call_kwargs["display_branch"] == "main"


def test_run_scan_cycle_trivy_respects_ticket_cap_override():
    from cli.scan_runner import run_scan_cycle

    config = make_trivy_config()
    config["ticket_cap"] = 5

    with (
        patch(f"{MODULE}.ensure_trivy"),
        patch(f"{MODULE}.TrivyClient") as mock_trivy_client_cls,
        patch(f"{MODULE}.trigger_workflow", new_callable=AsyncMock) as mock_trigger_workflow,
        patch(f"{MODULE}.detect_git_branch", return_value=None),
    ):
        mock_trivy_client_cls.return_value.fetch_findings.return_value = []
        mock_trigger_workflow.return_value = TicketResult()

        run_scan_cycle(config, "/some/repo", ticket_cap=10)  # CLI override wins over config's 5

    assert mock_trigger_workflow.call_args.kwargs["ticket_cap"] == 10


async def test_trigger_workflow_threads_pre_fetched_findings_and_null_ce_task_id():
    """workflow_trigger.trigger_workflow() itself: verifies the SonarToJiraInput it builds for Trivy's call shape."""
    from cli.scan_runner.workflow_trigger import trigger_workflow

    config = {
        "project_key": None,
        "scanner_type": "trivy",
        "scanner_mode": "local",
        "ticket_backend": "jira",
        "credentials": {},
        "branches": None,
    }
    findings = [make_finding("CVE-2018-1000656|flask|requirements.txt")]

    fake_client = MagicMock()
    fake_client.execute_workflow = AsyncMock(return_value=TicketResult())

    with patch("cli.scan_runner.workflow_trigger.Client.connect", new_callable=AsyncMock, return_value=fake_client):
        await trigger_workflow(
            config, ce_task_id=None, branch=None, display_branch="main", pre_fetched_findings=findings
        )

    call = fake_client.execute_workflow.call_args
    workflow_input = call.args[1]
    assert workflow_input.pre_fetched_findings == findings
    assert workflow_input.task_id is None
    # workflow_id falls back to a random id when ce_task_id is None, not a literal "None".
    assert "sonar-jira-None" not in call.kwargs.get("id", "") and "sonar-jira-" in call.kwargs.get("id", "")
