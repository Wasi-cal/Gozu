"""
Normalized domain vocabulary shared by every scanner adapter (scanner_client.py)
and every ticket adapter (ticket_client.py).

Neither side should ever see a tool-specific value (SonarQube's BLOCKER/MAJOR
severities, Jira's issue keys, etc) outside its own adapter - everything that
crosses the boundary between "fetch findings" and "create tickets" is expressed
in these types.
"""

from dataclasses import dataclass
from enum import Enum

from pydantic import BaseModel


class Severity(Enum):
    """
    Normalized severity vocabulary. Every scanner adapter translates its own
    tool-specific severity scale INTO this; every ticket adapter translates
    this OUT INTO its own tool-specific priority scale.
    """

    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    INFO = "INFO"


@dataclass
class Finding:
    """A single issue reported by a scanner, in tool-agnostic form."""

    key: str
    title: str
    severity: Severity
    component: str
    line: int | None
    message: str
    finding_type: str  # generic, e.g. "vulnerability" or "hotspot" - not tool-specific naming
    deep_link: str
    source_tool: str  # e.g. "sonarqube"


class CreatedTicket(BaseModel):
    """Pydantic (not a dataclass, unlike Finding) since this is an activity
    result type handled by Temporal's pydantic data converter directly."""

    finding_key: str
    ticket_key: str


class TicketResult(BaseModel):
    created: list[CreatedTicket] = []
    skipped: list[str] = []
