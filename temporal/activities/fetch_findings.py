"""Activity: fetch findings for a project via the ScannerClient interface."""

import dataclasses

from temporalio import activity

from core.models import Finding
from scanner.client import get_scanner_client


@activity.defn
async def fetch_findings_activity(project_key: str) -> list[dict]:
    """
    Returns findings as plain dicts (not Finding objects) because Finding is
    a dataclass with an Enum field, and Temporal's data converter doesn't
    serialize either of those on its own - create_tickets_activity
    reconstructs Finding objects from these dicts.
    """
    client = get_scanner_client()
    findings: list[Finding] = client.fetch_findings(project_key)

    results = []
    for finding in findings:
        data = dataclasses.asdict(finding)
        data["severity"] = finding.severity.value
        results.append(data)
    return results
