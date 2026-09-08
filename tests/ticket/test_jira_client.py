# Copyright (c) 2026 Calfus Inc.
# Author: Wasiullah Rafeeq S

from unittest.mock import MagicMock, patch

from core.errors import TicketValidationError
from core.models import Finding, Severity
from ticket.jira_client import (
    CUSTOM_FIELD_COMPONENT_NAME,
    CUSTOM_FIELD_LINE_NAME,
    JiraClient,
)


def make_finding(
    branch: str | None = None,
    finding_type: str = "vulnerability",
    line: int | None = 42,
    llm_explanation: str | None = None,
) -> Finding:
    return Finding(
        key="proj:src/app.py:1",
        title="Hardcoded credentials",
        severity=Severity.HIGH,
        component="proj:src/app.py",
        line=line,
        message="Don't hardcode credentials",
        finding_type=finding_type,
        deep_link="https://sonar.example.com/project/issues?id=proj&open=x",
        source_tool="sonarqube",
        branch=branch,
        llm_explanation=llm_explanation,
    )


def make_client() -> JiraClient:
    return JiraClient(base_url="https://jira.example.com", email="bot@example.com", api_token="tok", project_key="PROJ")


def make_response(status_code: int, json_body: dict | list) -> MagicMock:
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = json_body
    return response


# --- Labels (Part A) ---


def test_build_labels_without_branch_omits_branch_label():
    client = make_client()
    labels = client._build_labels(make_finding(branch=None))
    assert labels == ["source-key-proj:src/app.py:1"]
    assert not any(label.startswith("branch-") for label in labels)


def test_build_labels_with_branch_includes_normalized_branch_label():
    client = make_client()
    labels = client._build_labels(make_finding(branch="release/2.0"))
    assert "branch-release/2.0" in labels


def test_build_description_omits_moved_fields():
    client = make_client()
    description = client._build_description(
        make_finding(), component_moved=True, line_moved=True, severity_moved=True
    )
    text_nodes = [
        node["text"]
        for block in description["content"]
        if block["type"] == "bulletList"
        for item in block["content"]
        for para in item["content"]
        for node in para["content"]
    ]
    assert not any(t.startswith("Component:") for t in text_nodes)
    assert not any(t.startswith("Line:") for t in text_nodes)
    assert not any(t.startswith("Severity:") for t in text_nodes)
    assert any(t.startswith("Type:") for t in text_nodes)


def test_build_description_includes_how_to_fix_when_present():
    client = make_client()
    finding = make_finding()
    finding.how_to_fix = "Use tempfile.NamedTemporaryFile instead."
    description = client._build_description(finding)
    code_blocks = [block for block in description["content"] if block["type"] == "codeBlock"]
    assert len(code_blocks) == 1
    assert code_blocks[0]["content"][0]["text"] == "Use tempfile.NamedTemporaryFile instead."


def test_build_description_omits_how_to_fix_when_absent():
    client = make_client()
    description = client._build_description(make_finding())
    assert not any(block["type"] == "codeBlock" for block in description["content"])


def test_build_description_never_includes_deep_link():
    client = make_client()
    description = client._build_description(make_finding())
    rendered = str(description)
    assert "example.com/project/issues" not in rendered


def test_build_description_prefers_llm_explanation_over_message():
    client = make_client()
    description = client._build_description(make_finding(llm_explanation="This is risky because ..."))
    rendered = str(description)
    assert "This is risky because ..." in rendered
    assert "Don't hardcode credentials" not in rendered


def test_build_description_falls_back_to_message_when_no_llm_explanation():
    client = make_client()
    description = client._build_description(make_finding(llm_explanation=None))
    rendered = str(description)
    assert "Don't hardcode credentials" in rendered


# --- Custom field discovery (Part B, item 3) ---


def test_discover_custom_fields_returns_only_matches():
    client = make_client()
    field_list = [
        {"id": "customfield_10057", "name": CUSTOM_FIELD_COMPONENT_NAME},
        {"id": "customfield_10099", "name": "Some Unrelated Field"},
    ]
    with patch("ticket.jira_client.requests.get", return_value=make_response(200, field_list)) as mock_get:
        result = client.discover_custom_fields([CUSTOM_FIELD_COMPONENT_NAME, CUSTOM_FIELD_LINE_NAME])

    assert result == {CUSTOM_FIELD_COMPONENT_NAME: "customfield_10057"}
    assert CUSTOM_FIELD_LINE_NAME not in result
    mock_get.assert_called_once()
    assert mock_get.call_args.args[0] == "https://jira.example.com/rest/api/3/field"


def test_discover_custom_fields_empty_when_none_configured():
    client = make_client()
    with patch("ticket.jira_client.requests.get", return_value=make_response(200, [])):
        result = client.discover_custom_fields([CUSTOM_FIELD_COMPONENT_NAME, CUSTOM_FIELD_LINE_NAME])
    assert result == {}


# --- Payload construction: per-field, not all-or-nothing (item 5) ---


def test_build_create_payload_sets_only_discovered_fields():
    client = make_client()
    payload = client._build_create_payload(make_finding(), component_field_id="customfield_111", line_field_id=None)
    assert payload["fields"]["customfield_111"] == "proj:src/app.py"
    assert not any(k.startswith("customfield_") and k != "customfield_111" for k in payload["fields"])


def test_build_create_payload_skips_line_field_when_line_is_none():
    client = make_client()
    payload = client._build_create_payload(make_finding(line=None), component_field_id=None, line_field_id="customfield_222")
    assert "customfield_222" not in payload["fields"]
    # Falls back to Description since there was nothing real to set.
    assert "Line:" in str(payload["fields"]["description"])


# --- Write-time fallback: precise, not "any 400" (item 6) ---


def test_rejected_custom_field_ids_subset_match():
    client = make_client()
    response = make_response(400, {"errors": {"customfield_111": "not on the screen"}})
    rejected = client._rejected_custom_field_ids(response, {"customfield_111", "customfield_222"})
    assert rejected == {"customfield_111"}


def test_rejected_custom_field_ids_returns_empty_for_unrelated_error():
    """An error naming something OTHER than our candidate fields must never be treated as a custom-field rejection."""
    client = make_client()
    response = make_response(400, {"errors": {"project": "project is required"}})
    rejected = client._rejected_custom_field_ids(response, {"customfield_111"})
    assert rejected == set()


def test_rejected_custom_field_ids_returns_empty_for_mixed_errors():
    """One rejected custom field AND one unrelated error together must not trigger the fallback."""
    client = make_client()
    response = make_response(400, {"errors": {"customfield_111": "not on the screen", "project": "invalid"}})
    rejected = client._rejected_custom_field_ids(response, {"customfield_111"})
    assert rejected == set()


def test_create_ticket_retries_without_rejected_custom_field():
    client = make_client()
    client._sprints.add_issue = MagicMock()

    rejected_response = make_response(400, {"errors": {"customfield_111": "not on the screen"}})
    success_response = make_response(201, {"key": "PROJ-1"})
    remote_link_response = make_response(201, {})

    with patch(
        "ticket.jira_client.requests.post", side_effect=[rejected_response, success_response, remote_link_response]
    ) as mock_post:
        issue_key = client.create_ticket(make_finding(), custom_fields={CUSTOM_FIELD_COMPONENT_NAME: "customfield_111"})

    assert issue_key == "PROJ-1"
    assert mock_post.call_count == 3  # rejected create, retried create, remote link
    retried_payload = mock_post.call_args_list[1].kwargs["json"]
    assert "customfield_111" not in retried_payload["fields"]
    assert "Component:" in str(retried_payload["fields"]["description"])


def test_create_ticket_unrelated_400_raises_without_retry():
    client = make_client()
    unrelated_response = make_response(400, {"errors": {"project": "project is required"}})
    unrelated_response.text = '{"errors": {"project": "project is required"}}'

    with patch("ticket.jira_client.requests.post", return_value=unrelated_response) as mock_post:
        try:
            client.create_ticket(make_finding(), custom_fields={CUSTOM_FIELD_COMPONENT_NAME: "customfield_111"})
            assert False, "expected TicketValidationError"
        except TicketValidationError:
            pass

    assert mock_post.call_count == 1  # no retry attempted


# --- Remote link (item 7) ---


def test_create_ticket_creates_remote_link_with_deep_link():
    client = make_client()
    client._sprints.add_issue = MagicMock()

    create_response = make_response(201, {"key": "PROJ-2"})
    remote_link_response = make_response(201, {})

    with patch("ticket.jira_client.requests.post", side_effect=[create_response, remote_link_response]) as mock_post:
        client.create_ticket(make_finding())

    remote_link_call = mock_post.call_args_list[1]
    assert remote_link_call.args[0] == "https://jira.example.com/rest/api/3/issue/PROJ-2/remotelink"
    assert remote_link_call.kwargs["json"]["object"]["url"] == make_finding().deep_link


def test_create_ticket_remote_link_failure_does_not_fail_creation():
    """Best-effort, same treatment as sprint assignment - a remote-link error must never fail create_ticket()."""
    client = make_client()
    client._sprints.add_issue = MagicMock()

    create_response = make_response(201, {"key": "PROJ-3"})
    with patch("ticket.jira_client.requests.post", side_effect=[create_response, RuntimeError("network blip")]):
        issue_key = client.create_ticket(make_finding())

    assert issue_key == "PROJ-3"
