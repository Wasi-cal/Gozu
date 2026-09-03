"""Activity: create a ticket for each finding that doesn't already have one."""

from temporalio import activity

from core.models import CreatedTicket, TicketResult
from temporal.models.create_tickets import CreateTicketsInput
from ticket.client import build_ticket_client, get_ticket_client


@activity.defn
async def create_tickets_activity(input: CreateTicketsInput) -> TicketResult:
    """
    Dedupe is based on the "source-key-{key}" label set on the ticket at
    creation time.
    """
    client = (
        build_ticket_client(input.ticket_backend, input.credentials) if input.credentials else get_ticket_client()
    )

    created = []
    skipped = []

    for finding in input.findings:
        existing_ticket = client.find_existing(finding.key)
        if existing_ticket:
            skipped.append(finding.key)
            continue

        ticket_key = client.create_ticket(finding)
        created.append(CreatedTicket(finding_key=finding.key, ticket_key=ticket_key))

    return TicketResult(created=created, skipped=skipped)
