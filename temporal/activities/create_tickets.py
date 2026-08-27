"""Activity: create a ticket for each finding that doesn't already have one."""

from temporalio import activity

from models import CreatedTicket, Finding, Severity, TicketResult
from ticket_client import get_ticket_client


@activity.defn
async def create_tickets_activity(findings: list[dict]) -> TicketResult:
    """
    Dedupe is based on the "source-key-{key}" label set on the ticket at
    creation time.
    """
    client = get_ticket_client()

    created = []
    skipped = []

    for data in findings:
        finding = Finding(**{**data, "severity": Severity(data["severity"])})

        existing_ticket = client.find_existing(finding.key)
        if existing_ticket:
            skipped.append(finding.key)
            continue

        ticket_key = client.create_ticket(finding)
        created.append(CreatedTicket(finding_key=finding.key, ticket_key=ticket_key))

    return TicketResult(created=created, skipped=skipped)
