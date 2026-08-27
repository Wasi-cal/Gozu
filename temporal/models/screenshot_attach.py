"""Input model for capture_and_attach_screenshot_activity."""

from pydantic import BaseModel

from sonar.models import SonarIssue


class ScreenshotAttachInput(BaseModel):
    sonar_issue: SonarIssue
    jira_issue_key: str
