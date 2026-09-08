# Copyright (c) 2026 Calfus Inc.
# Author: Prakrit Mohanty

from unittest.mock import MagicMock, patch

from anthropic.types import TextBlock

from core.models import Finding, Severity
from llm.enrich import enrich_finding, generate_explanation

MODULE = "llm.enrich"


def make_finding(rule_key: str | None = "python:S5443", how_to_fix: str | None = "Use tempfile instead.") -> Finding:
    return Finding(
        key="proj:src/app.py:1",
        title="Insecure temp file",
        severity=Severity.HIGH,
        component="proj:src/app.py",
        line=42,
        message="Don't use a predictable temp file name",
        finding_type="vulnerability",
        deep_link="https://sonar.example.com/project/issues?id=proj&issues=x",
        source_tool="sonarqube",
        rule_key=rule_key,
        how_to_fix=how_to_fix,
    )


def make_client(text: str = "This is risky because ...") -> MagicMock:
    client = MagicMock()
    response = MagicMock()
    response.content = [MagicMock(spec=TextBlock, text=text)]
    client.messages.create.return_value = response
    return client


def test_generate_explanation_includes_rule_message_and_snippet_in_prompt():
    client = make_client("Explanation text")
    result = generate_explanation(client, make_finding(), "os.tempnam()")

    assert result == "Explanation text"
    call_kwargs = client.messages.create.call_args.kwargs
    prompt = call_kwargs["messages"][0]["content"]
    assert "Don't use a predictable temp file name" in prompt
    assert "Use tempfile instead." in prompt
    assert "os.tempnam()" in prompt


def test_generate_explanation_without_snippet_uses_generic_unavailable_note():
    """A missing snippet isn't automatically a credential concern - see llm/enrich.py's two distinct notes."""
    client = make_client()
    generate_explanation(client, make_finding(), None)

    prompt = client.messages.create.call_args.kwargs["messages"][0]["content"]
    assert "not available" in prompt.lower()
    assert "credential" not in prompt.lower()


def test_generate_explanation_propagates_api_errors():
    client = make_client()
    client.messages.create.side_effect = RuntimeError("rate limited")

    try:
        generate_explanation(client, make_finding(), None)
        assert False, "expected RuntimeError to propagate"
    except RuntimeError:
        pass


def test_enrich_finding_includes_snippet_for_non_credential_rule():
    client = make_client()
    with patch(f"{MODULE}.fetch_snippet_lines", return_value=(["line1", "line2"], 40)) as mock_fetch:
        enrich_finding(client, make_finding(rule_key="python:S5443"), "tok")

    mock_fetch.assert_called_once()
    prompt = client.messages.create.call_args.kwargs["messages"][0]["content"]
    assert "line1" in prompt and "line2" in prompt


def test_enrich_finding_withholds_snippet_for_generic_hardcoded_credentials_rule():
    """python:S2068 - 'Credentials should not be hard-coded' - present per-language."""
    client = make_client()
    with patch(f"{MODULE}.fetch_snippet_lines") as mock_fetch:
        enrich_finding(client, make_finding(rule_key="python:S2068"), "tok")

    mock_fetch.assert_not_called()
    prompt = client.messages.create.call_args.kwargs["messages"][0]["content"]
    assert "withheld" in prompt.lower()


def test_enrich_finding_withholds_snippet_for_dedicated_secrets_engine_rule():
    client = make_client()
    with patch(f"{MODULE}.fetch_snippet_lines") as mock_fetch:
        enrich_finding(client, make_finding(rule_key="secrets:S6290"), "tok")

    mock_fetch.assert_not_called()


def test_enrich_finding_falls_back_to_no_snippet_when_fetch_fails():
    client = make_client()
    with patch(f"{MODULE}.fetch_snippet_lines", side_effect=RuntimeError("source file not found")):
        result = enrich_finding(client, make_finding(rule_key="python:S5443"), "tok")

    assert result == "This is risky because ..."
    prompt = client.messages.create.call_args.kwargs["messages"][0]["content"]
    assert "withheld" not in prompt.lower()  # only the credential-rule path says "withheld"
