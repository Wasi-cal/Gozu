# Copyright (c) 2026 Calfus Inc.
# Author: Prakrit Mohanty
# Editor: Wasiullah Rafeeq S

"""Input model for capture_and_attach_screenshot_activity."""

from pydantic import BaseModel

from core.models import Finding


class ScreenshotAttachInput(BaseModel):
    finding: Finding
    ticket_key: str
    ticket_backend: str = "jira"
    # Shared with the workflow's `credentials` - carries both the ticket
    # backend's credentials (attach_screenshot/add_comment) and sonar_token
    # (capture_finding_screenshot's Basic auth). Empty falls back to
    # get_ticket_client()/SONAR_TOKEN, same as SonarToJiraInput.
    credentials: dict[str, str] = {}
