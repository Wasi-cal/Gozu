"""Data models for SonarQube issues."""

from pydantic import BaseModel


class SonarIssue(BaseModel):
    key: str
    rule: str
    severity: str
    component: str
    line: int | None
    message: str
    type: str  # "VULNERABILITY" or "SECURITY_HOTSPOT"
    deep_link: str
