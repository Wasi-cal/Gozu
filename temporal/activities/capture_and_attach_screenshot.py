"""Activity: capture a screenshot of a finding and attach it to its ticket."""

import os
import shutil
import tempfile
from pathlib import Path

from temporalio import activity

from scanner.screenshot import capture_finding_screenshot
from temporal.models.screenshot_attach import ScreenshotAttachInput
from ticket.client import build_ticket_client, get_ticket_client


@activity.defn
async def capture_and_attach_screenshot_activity(input: ScreenshotAttachInput) -> None:
    finding = input.finding

    tmp_dir = tempfile.mkdtemp(prefix="finding-screenshot-")
    screenshot_path = Path(tmp_dir) / f"{finding.source_tool}-{finding.key}.png"

    try:
        token = input.credentials.get("sonar_token", os.environ.get("SONAR_TOKEN", ""))
        extraction = await capture_finding_screenshot(finding, screenshot_path, token)

        client = build_ticket_client(input.ticket_backend, input.credentials) if input.credentials else get_ticket_client()

        attach_screenshot = getattr(client, "attach_screenshot", None)
        if attach_screenshot is not None:
            attach_screenshot(input.ticket_key, extraction.screenshot_path)
        else:
            activity.logger.warning(
                f"{type(client).__name__} doesn't support attach_screenshot; skipping for {input.ticket_key}"
            )

        if extraction.code_snippet or extraction.annotation_text:
            add_comment = getattr(client, "add_comment", None)
            if add_comment is not None:
                body_lines = [t for t in (extraction.annotation_text, extraction.code_snippet) if t]
                add_comment(input.ticket_key, "\n\n".join(body_lines))
            else:
                activity.logger.warning(
                    f"{type(client).__name__} doesn't support add_comment; skipping comment for {input.ticket_key}"
                )
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
