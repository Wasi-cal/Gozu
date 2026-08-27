"""Activity: capture a screenshot of a Sonar issue and attach it to its Jira ticket."""

import os
import shutil
import tempfile
from pathlib import Path

from temporalio import activity

from jira.client import JiraClient
from sonar.screenshot import capture_issue_screenshot
from temporal.models.screenshot_attach import ScreenshotAttachInput


@activity.defn
async def capture_and_attach_screenshot_activity(input: ScreenshotAttachInput) -> None:
    tmp_dir = tempfile.mkdtemp(prefix="sonar-screenshot-")
    screenshot_path = Path(tmp_dir) / f"sonar-{input.sonar_issue.key}.png"

    try:
        await capture_issue_screenshot(input.sonar_issue, screenshot_path)

        client = JiraClient(
            base_url=os.environ["JIRA_URL"],
            email=os.environ["JIRA_EMAIL"],
            api_token=os.environ["JIRA_API_TOKEN"],
            project_key=os.environ["JIRA_PROJECT_KEY"],
        )
        client.attach_screenshot(input.jira_issue_key, screenshot_path)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
