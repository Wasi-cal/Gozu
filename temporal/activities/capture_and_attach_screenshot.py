"""Activity: capture a screenshot of a Sonar issue and attach it to its Jira ticket."""

import logging
import os
import shutil
import tempfile
from pathlib import Path

from temporalio import activity

from jira.client import JiraClient
from sonar.screenshot import capture_issue_screenshot
from temporal.models.screenshot_attach import ScreenshotAttachInput

logger = logging.getLogger(__name__)


@activity.defn
async def capture_and_attach_screenshot_activity(input: ScreenshotAttachInput) -> None:
    tmp_dir = tempfile.mkdtemp(prefix="sonar-screenshot-")
    screenshot_path = Path(tmp_dir) / f"sonar-{input.sonar_issue.key}.png"

    try:
        extraction = await capture_issue_screenshot(input.sonar_issue, screenshot_path)

        client = JiraClient(
            base_url=os.environ["JIRA_URL"],
            email=os.environ["JIRA_EMAIL"],
            api_token=os.environ["JIRA_API_TOKEN"],
            project_key=os.environ["JIRA_PROJECT_KEY"],
        )

        attach_screenshot_fn = getattr(client, "attach_screenshot", None)
        if attach_screenshot_fn is not None:
            attach_screenshot_fn(input.jira_issue_key, extraction.screenshot_path)
        else:
            logger.warning(f"Client has no attach_screenshot method; skipping for {input.jira_issue_key}")

        if extraction.code_snippet or extraction.annotation_text:
            add_comment_fn = getattr(client, "add_comment", None)
            if add_comment_fn is not None:
                body_lines = [t for t in (extraction.annotation_text, extraction.code_snippet) if t]
                add_comment_fn(input.jira_issue_key, "\n\n".join(body_lines))
            else:
                logger.warning(f"Client has no add_comment method; skipping comment for {input.jira_issue_key}")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
