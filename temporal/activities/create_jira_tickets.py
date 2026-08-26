"""Activity: create a Jira ticket for each Sonar issue that doesn't already have one."""

import os

from temporalio import activity

from jira.client import JiraClient
from jira.models import CreatedTicket, JiraTicketResult
from sonar.models import SonarIssue


@activity.defn
async def create_jira_tickets_activity(vulnerabilities: list[SonarIssue]) -> JiraTicketResult:
    """
    Dedupe is based on the "sonar-key-{key}" label set on the ticket at
    creation time.
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
        existing_ticket = client.find_existing_ticket(issue.key)

        if existing_ticket:
            skipped.append(issue.key)
            continue

        jira_key = client.create_ticket(issue)
        created.append(CreatedTicket(sonar_key=issue.key, jira_key=jira_key))

    return JiraTicketResult(created=created, skipped=skipped)
