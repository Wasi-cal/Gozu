"""Activity: capture a screenshot of a finding and attach it to its ticket."""

import logging
import shutil
import tempfile
from pathlib import Path

from temporalio import activity

from core.models import Finding, Severity
from scanner.screenshot import capture_finding_screenshot
from temporal.models.screenshot_attach import ScreenshotAttachInput
from ticket.client import get_ticket_client

logger = logging.getLogger(__name__)


@activity.defn
async def capture_and_attach_screenshot_activity(input: ScreenshotAttachInput) -> None:
    finding = Finding(**{**input.finding, "severity": Severity(input.finding["severity"])})

    tmp_dir = tempfile.mkdtemp(prefix="finding-screenshot-")
    screenshot_path = Path(tmp_dir) / f"{finding.source_tool}-{finding.key}.png"

    try:
        await capture_finding_screenshot(finding, screenshot_path)

        client = get_ticket_client()
        attach_screenshot = getattr(client, "attach_screenshot", None)
        if attach_screenshot is None:
            logger.warning(
                f"{type(client).__name__} doesn't support attach_screenshot; skipping for {input.ticket_key}"
            )
            return
        attach_screenshot(input.ticket_key, screenshot_path)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
