# Copyright (c) 2026 Calfus Inc.
# Author: Wasiullah Rafeeq S

"""
Integration test: scan (TrivyClient, mocked subprocess) -> normalize
(Finding) -> dedup (ticket/claims.py, faked - no real Postgres) ->
Jira ticket creation (create_tickets_activity), using Trivy as the
source. Deliberately does NOT go through scanner/factory.py or any
Temporal workflow - build_scanner_client() has no "trivy" branch yet
(that's later Temporal/CLI wiring, a separate step); this proves the
scan->normalize->dedup->ticket pipeline itself already works end to end
for a Trivy-sourced Finding, since create_tickets_activity only ever
consumes a plain list[Finding] and has no scanner-specific code path at
all - it doesn't need to know or care which scanner produced them.
"""

import json
from unittest.mock import MagicMock, patch

from scanner.trivy_client import TrivyClient
from temporal.activities.create_tickets import create_tickets_activity
from temporal.models.create_tickets import CreateTicketsInput

MODULE = "temporal.activities.create_tickets"

# Same real captured `trivy fs --format json` extract used in
# tests/scanner/test_trivy_client.py.
REAL_TRIVY_OUTPUT = {
    "Results": [
        {
            "Target": "requirements.txt",
            "Vulnerabilities": [
                {
                    "VulnerabilityID": "CVE-2018-1000656",
                    "PkgName": "flask",
                    "InstalledVersion": "0.12",
                    "FixedVersion": "0.12.3",
                    "Severity": "HIGH",
                    "PrimaryURL": "https://avd.aquasec.com/nvd/cve-2018-1000656",
                    "Title": "python-flask: Denial of Service via crafted JSON file",
                },
                {
                    "VulnerabilityID": "CVE-2018-18074",
                    "PkgName": "requests",
                    "InstalledVersion": "2.6.0",
                    "FixedVersion": "2.20.0",
                    "Severity": "HIGH",
                    "PrimaryURL": "https://avd.aquasec.com/nvd/cve-2018-18074",
                    "Title": "python-requests: Redirect leaking Authorization header",
                },
            ],
        }
    ]
}


def make_ticket_client() -> MagicMock:
    client = MagicMock()
    client.destination_id.return_value = "jira:https://example.atlassian.net:PROJ"
    client.find_existing.return_value = None
    client.upsert_rollup_ticket.return_value = None
    client.discover_custom_fields.return_value = {}
    client.create_ticket.side_effect = lambda finding, custom_fields=None: f"PROJ-{finding.package_name}"
    return client


def patched_claims():
    claims = MagicMock()
    claims.get_connection.return_value.__enter__.return_value = MagicMock()
    claims.get_ticket.return_value = None
    claims.claim.return_value = True
    return patch(f"{MODULE}.claims", claims)


async def test_trivy_findings_flow_through_dedup_to_jira_ticket_creation():
    with patch("subprocess.run", return_value=MagicMock(returncode=0, stdout=json.dumps(REAL_TRIVY_OUTPUT), stderr="")):
        findings = TrivyClient().fetch_findings("/some/repo")
    assert len(findings) == 2
    assert all(f.source_tool == "trivy" for f in findings)

    ticket_client = make_ticket_client()
    with patched_claims(), patch(f"{MODULE}.get_ticket_client", return_value=ticket_client):
        result = await create_tickets_activity(CreateTicketsInput(findings=findings))

    assert len(result.created) == 2
    created_keys = {c.finding_key for c in result.created}
    assert created_keys == {f.key for f in findings}
    assert ticket_client.create_ticket.call_count == 2

    # The Finding objects create_tickets_activity actually handed to
    # create_ticket() still carry Trivy's package-level fields intact -
    # nothing in the dedup/cap/rollup machinery strips or mutates them
    # on the way through.
    passed_findings = [call.args[0] for call in ticket_client.create_ticket.call_args_list]
    assert {f.package_name for f in passed_findings} == {"flask", "requests"}
    assert all(f.line is None for f in passed_findings)


async def test_trivy_finding_already_claimed_is_skipped_not_recreated():
    """Same ledger, same code path as SonarQube - a Trivy finding_key already claimed is skipped, not double-ticketed."""
    with patch("subprocess.run", return_value=MagicMock(returncode=0, stdout=json.dumps(REAL_TRIVY_OUTPUT), stderr="")):
        findings = TrivyClient().fetch_findings("/some/repo")

    ticket_client = make_ticket_client()
    claims = MagicMock()
    claims.get_connection.return_value.__enter__.return_value = MagicMock()
    claims.get_ticket.return_value = "PROJ-999"  # already ticketed
    with patch(f"{MODULE}.claims", claims), patch(f"{MODULE}.get_ticket_client", return_value=ticket_client):
        result = await create_tickets_activity(CreateTicketsInput(findings=findings))

    assert result.created == []
    assert set(result.skipped) == {f.key for f in findings}
    ticket_client.create_ticket.assert_not_called()
