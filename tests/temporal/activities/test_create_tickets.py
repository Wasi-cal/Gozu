# Copyright (c) 2026 Calfus Inc.
# Author: Wasiullah Rafeeq S

from unittest.mock import MagicMock, patch

from core.models import Finding, Severity
from temporal.activities.create_tickets import create_tickets_activity
from temporal.models.create_tickets import CreateTicketsInput

MODULE = "temporal.activities.create_tickets"


def make_finding(key: str) -> Finding:
    return Finding(
        key=key,
        title="Some vulnerability",
        severity=Severity.HIGH,
        component="proj:file.py",
        line=1,
        message="some message",
        finding_type="vulnerability",
        deep_link="http://localhost:9000/project/issues?id=proj",
        source_tool="sonarqube",
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
