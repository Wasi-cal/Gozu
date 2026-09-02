"""Input model for capture_and_attach_screenshot_activity."""

from pydantic import BaseModel

from core.models import Finding


class ScreenshotAttachInput(BaseModel):
    finding: Finding
    ticket_key: str
