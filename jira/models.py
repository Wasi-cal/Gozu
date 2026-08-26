"""Data models for the Jira ticket-creation activity's result."""

from pydantic import BaseModel


class CreatedTicket(BaseModel):
    sonar_key: str
    jira_key: str


class JiraTicketResult(BaseModel):
    created: list[CreatedTicket] = []
    skipped: list[str] = []
