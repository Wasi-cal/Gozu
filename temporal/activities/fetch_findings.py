"""Activity: fetch findings for a project via the ScannerClient interface."""

from temporalio import activity

from core.models import Finding
from scanner.client import get_scanner_client


@activity.defn
async def fetch_findings_activity(project_key: str) -> list[Finding]:
    client = get_scanner_client()
    return client.fetch_findings(project_key)
