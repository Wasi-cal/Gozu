# Copyright (c) 2026 Calfus Inc.
# Author: Wasiullah Rafeeq S
# Editor: Prakrit Mohanty

from unittest.mock import MagicMock, patch

from core.models import Finding, Severity
from temporal.activities.create_tickets import (
    _add_llm_explanation,
    create_tickets_activity,
)
from temporal.models.create_tickets import CreateTicketsInput

MODULE = "temporal.activities.create_tickets"


def make_finding(key: str, severity: Severity = Severity.HIGH) -> Finding:
    return Finding(
        key=key,
        title="Some vulnerability",
        severity=severity,
        component="proj:file.py",
        line=1,
        message="some message",
        finding_type="vulnerability",
        deep_link="http://localhost:9000/project/issues?id=proj",
        source_tool="sonarqube",
        rule_key="python:S5443",
    )


def make_client(create_ticket_side_effect=None):
    """A ticket client double with no claims/Postgres involvement - claims module is patched separately."""
    client = MagicMock()
    client.destination_id.return_value = "jira:https://example.atlassian.net:PROJ"
    client.find_existing.return_value = None
    client.upsert_rollup_ticket.return_value = None
    if create_ticket_side_effect is not None:
        client.create_ticket.side_effect = create_ticket_side_effect
    else:
        client.create_ticket.side_effect = lambda finding, custom_fields=None: f"PROJ-{finding.key}"
    return client


def patched_claims():
    """
    Fakes the whole ticket/claims.py module used by create_tickets_activity -
    a real connection would need real Postgres, and this activity's own
    claim-ledger semantics aren't what these tests are about.
    """
    claims = MagicMock()
    claims.get_connection.return_value.__enter__.return_value = MagicMock()
    claims.get_ticket.return_value = None
    claims.claim.return_value = True
    return patch(f"{MODULE}.claims", claims)


async def test_ticket_cap_from_input_defers_findings_beyond_it():
    """The whole point of the new configurable cap: input.ticket_cap overrides BACKLOG_CAP for this run."""
    findings = [make_finding("k1"), make_finding("k2"), make_finding("k3")]
    client = make_client()

    with patched_claims(), patch(f"{MODULE}.get_ticket_client", return_value=client):
        result = await create_tickets_activity(CreateTicketsInput(findings=findings, ticket_cap=1))

    assert len(result.created) == 1
    assert len(result.deferred) == 2
    client.create_ticket.assert_called_once()


async def test_ticket_cap_zero_defers_everything_without_any_create_ticket_call():
    """The exact scenario proven live: --ticket-cap 0 means zero Jira create calls at all, not just a low cap."""
    findings = [make_finding("k1"), make_finding("k2")]
    client = make_client()

    with patched_claims(), patch(f"{MODULE}.get_ticket_client", return_value=client):
        result = await create_tickets_activity(CreateTicketsInput(findings=findings, ticket_cap=0))

    assert result.created == []
    assert result.deferred == ["k1", "k2"]
    client.create_ticket.assert_not_called()


async def test_ticket_cap_none_falls_back_to_backlog_cap_constant():
    from temporal.activities.create_tickets import BACKLOG_CAP

    findings = [make_finding(f"k{i}") for i in range(BACKLOG_CAP + 2)]
    client = make_client()

    with patched_claims(), patch(f"{MODULE}.get_ticket_client", return_value=client):
        result = await create_tickets_activity(CreateTicketsInput(findings=findings, ticket_cap=None))

    assert len(result.created) == BACKLOG_CAP
    assert len(result.deferred) == 2


async def test_custom_field_discovery_failure_is_caught_and_activity_still_completes():
    """
    Confirmed live: an unwrapped discover_custom_fields() failure (a real
    RuntimeError from an unreachable/misconfigured Jira instance) used to
    fail this entire activity before a single finding was even looked at,
    regardless of ticket_cap. Must be caught, logged, and fall back to {}.
    """
    findings = [make_finding("k1")]
    client = make_client()
    client.discover_custom_fields.side_effect = RuntimeError("Jira list fields failed with status 404")

    with patched_claims(), patch(f"{MODULE}.get_ticket_client", return_value=client):
        result = await create_tickets_activity(CreateTicketsInput(findings=findings))

    assert len(result.created) == 1
    client.discover_custom_fields.assert_called_once()
    # create_ticket still got called with an empty custom_fields dict - the fallback.
    call_kwargs = client.create_ticket.call_args
    assert call_kwargs.args[1] == {} or call_kwargs.kwargs.get("custom_fields") == {}


async def test_discover_custom_fields_only_called_once_per_activity_not_per_finding():
    findings = [make_finding("k1"), make_finding("k2"), make_finding("k3")]
    client = make_client()
    client.discover_custom_fields.return_value = {}

    with patched_claims(), patch(f"{MODULE}.get_ticket_client", return_value=client):
        await create_tickets_activity(CreateTicketsInput(findings=findings))

    client.discover_custom_fields.assert_called_once()


# --- _add_llm_explanation (LLM enrichment gating) ---


def test_add_llm_explanation_sets_field_for_high_severity_with_key_configured():
    finding = make_finding("k1", severity=Severity.HIGH)
    llm_client = MagicMock()

    with patch(f"{MODULE}.build_llm_client", return_value=llm_client) as mock_build, \
         patch(f"{MODULE}.enrich_finding", return_value="Explanation") as mock_enrich:
        _add_llm_explanation(finding, {"anthropic_api_key": "sk-ant-...", "sonar_token": "tok"})

    assert finding.llm_explanation == "Explanation"
    mock_build.assert_called_once_with("sk-ant-...")
    mock_enrich.assert_called_once_with(llm_client, finding, "tok")


def test_add_llm_explanation_sets_field_for_critical_severity():
    finding = make_finding("k1", severity=Severity.CRITICAL)

    with patch(f"{MODULE}.build_llm_client", return_value=MagicMock()), \
         patch(f"{MODULE}.enrich_finding", return_value="Explanation"):
        _add_llm_explanation(finding, {"anthropic_api_key": "sk-ant-..."})

    assert finding.llm_explanation == "Explanation"


def test_add_llm_explanation_skips_medium_severity():
    finding = make_finding("k1", severity=Severity.MEDIUM)

    with patch(f"{MODULE}.enrich_finding") as mock_enrich:
        _add_llm_explanation(finding, {"anthropic_api_key": "sk-ant-..."})

    mock_enrich.assert_not_called()
    assert finding.llm_explanation is None


def test_add_llm_explanation_skips_low_severity():
    finding = make_finding("k1", severity=Severity.LOW)

    with patch(f"{MODULE}.enrich_finding") as mock_enrich:
        _add_llm_explanation(finding, {"anthropic_api_key": "sk-ant-..."})

    mock_enrich.assert_not_called()


def test_add_llm_explanation_skips_when_no_anthropic_key_configured():
    finding = make_finding("k1", severity=Severity.HIGH)

    with patch(f"{MODULE}.enrich_finding") as mock_enrich:
        _add_llm_explanation(finding, {"sonar_token": "tok"})

    mock_enrich.assert_not_called()
    assert finding.llm_explanation is None


def test_add_llm_explanation_failure_is_caught_and_leaves_field_none():
    finding = make_finding("k1", severity=Severity.HIGH)

    with patch(f"{MODULE}.build_llm_client", side_effect=RuntimeError("bad key")):
        _add_llm_explanation(finding, {"anthropic_api_key": "sk-ant-..."})  # must not raise

    assert finding.llm_explanation is None


async def test_llm_explanation_is_set_before_create_ticket_is_called():
    """Placement matters: enrichment must run only for a genuinely-new ticket, right before create_ticket()."""
    finding = make_finding("k1", severity=Severity.HIGH)
    client = make_client()
    seen_explanation_at_create_time = []
    client.create_ticket.side_effect = lambda f, custom_fields=None: (
        seen_explanation_at_create_time.append(f.llm_explanation) or "PROJ-1"
    )

    with patched_claims(), \
         patch(f"{MODULE}.build_ticket_client", return_value=client), \
         patch(f"{MODULE}.build_llm_client", return_value=MagicMock()), \
         patch(f"{MODULE}.enrich_finding", return_value="Explanation"):
        await create_tickets_activity(
            CreateTicketsInput(findings=[finding], credentials={"anthropic_api_key": "sk-ant-..."})
        )

    assert seen_explanation_at_create_time == ["Explanation"]


async def test_llm_explanation_not_generated_without_anthropic_key_in_credentials():
    finding = make_finding("k1", severity=Severity.HIGH)
    client = make_client()

    with patched_claims(), \
         patch(f"{MODULE}.build_ticket_client", return_value=client), \
         patch(f"{MODULE}.enrich_finding") as mock_enrich:
        await create_tickets_activity(CreateTicketsInput(findings=[finding], credentials={"sonar_token": "tok"}))

    mock_enrich.assert_not_called()
