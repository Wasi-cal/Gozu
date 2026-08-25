"""Temporal activities. Activities are where side effects (HTTP calls, I/O) live."""

import dataclasses
import os

from temporalio import activity

from jira_client import JiraClient
from sonar_client import get_sonar_client


@activity.defn
async def fetch_vulnerabilities_activity(project_key: str) -> list[dict]:
    """
    Fetch vulnerabilities + security hotspots for a project via the
    Sonar interface, and return them as plain dicts (Temporal payloads
    have to be JSON-serializable, dataclasses aren't).
    """
    client = get_sonar_client()

    vulnerabilities = client.fetch_vulnerabilities(project_key)
    hotspots = client.fetch_hotspots(project_key)

    combined = vulnerabilities + hotspots
    return [dataclasses.asdict(issue) for issue in combined]


@activity.defn
async def create_jira_tickets_activity(vulnerabilities: list[dict]) -> dict:
    """
    Create a Jira ticket for each Sonar issue that doesn't already have
    one. Dedupe is based on the "sonar-key-{key}" label set on the ticket
    at creation time.
    """
    client = JiraClient(
        base_url=os.environ["JIRA_URL"],
        email=os.environ["JIRA_EMAIL"],
        api_token=os.environ["JIRA_API_TOKEN"],
        project_key=os.environ["JIRA_PROJECT_KEY"],
    )

    created = []
    skipped = []

    for issue in vulnerabilities:
        sonar_key = issue["key"]
        existing_ticket = client.find_existing_ticket(sonar_key)

        if existing_ticket:
            skipped.append(sonar_key)
            continue

        jira_key = client.create_ticket(issue)
        created.append({"sonar_key": sonar_key, "jira_key": jira_key})

    return {"created": created, "skipped": skipped}
