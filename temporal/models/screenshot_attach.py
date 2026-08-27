"""Input model for capture_and_attach_screenshot_activity."""

from pydantic import BaseModel


class ScreenshotAttachInput(BaseModel):
    finding: dict  # the same dict shape fetch_findings_activity returns (Finding, asdict'd)
    ticket_key: str
