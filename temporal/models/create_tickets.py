"""Input model for create_tickets_activity."""

from pydantic import BaseModel

from core.models import Finding


class CreateTicketsInput(BaseModel):
    findings: list[Finding]
    ticket_backend: str = "jira"
    credentials: dict[str, str] = {}
