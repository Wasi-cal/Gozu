"""Data models for SonarQube issues."""

from pydantic import BaseModel


class SonarIssue(BaseModel):
    key: str
    rule: str
    rule_name: str  # human-readable rule name (e.g. "Cognitive Complexity"); falls back to `rule` if lookup fails
    severity: str
    component: str
    line: int | None
    message: str
    type: str  # "VULNERABILITY" or "SECURITY_HOTSPOT"
    deep_link: str
