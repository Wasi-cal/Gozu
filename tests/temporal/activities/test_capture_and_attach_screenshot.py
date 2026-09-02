import os
import tempfile as tempfile_module
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sonar.models import SonarIssue
from sonar.screenshot import FindingExtraction
from temporal.activities.capture_and_attach_screenshot import capture_and_attach_screenshot_activity
from temporal.models.screenshot_attach import ScreenshotAttachInput

MODULE = "temporal.activities.capture_and_attach_screenshot"


@pytest.fixture(autouse=True)
def jira_env(monkeypatch):
    monkeypatch.setenv("JIRA_URL", "https://example.atlassian.net")
    monkeypatch.setenv("JIRA_EMAIL", "bot@example.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "token")
    monkeypatch.setenv("JIRA_PROJECT_KEY", "PROJ")


def make_input() -> ScreenshotAttachInput:
    return ScreenshotAttachInput(
        sonar_issue=SonarIssue(
            key="ABC-1",
            rule="python:S1",
            severity="MAJOR",
            component="proj:file.py",
            line=1,
            message="some message",
            type="VULNERABILITY",
            deep_link="http://localhost:9090/project/issues?id=proj",
        ),
        jira_issue_key="PROJ-1",
    )


def patched_extraction(code_snippet=None, annotation_text=None):
    async def fake_capture(issue, out_path):
        return FindingExtraction(screenshot_path=out_path, code_snippet=code_snippet, annotation_text=annotation_text)

    return patch(f"{MODULE}.capture_issue_screenshot", side_effect=fake_capture)


async def test_activity_calls_add_comment_when_extraction_succeeds():
    client = MagicMock()
    with patched_extraction(code_snippet="the code", annotation_text="the annotation"), \
         patch(f"{MODULE}.JiraClient", return_value=client):
        await capture_and_attach_screenshot_activity(make_input())

    client.attach_screenshot.assert_called_once()
    client.add_comment.assert_called_once()
    args, _ = client.add_comment.call_args
    assert args[0] == "PROJ-1"
    assert "the code" in args[1]
    assert "the annotation" in args[1]


async def test_activity_skips_add_comment_when_client_lacks_method(caplog):
    client = MagicMock(spec=["attach_screenshot"])
    with patched_extraction(code_snippet="the code", annotation_text=None), \
         patch(f"{MODULE}.JiraClient", return_value=client):
        await capture_and_attach_screenshot_activity(make_input())

    client.attach_screenshot.assert_called_once()
    assert "no add_comment method" in caplog.text


async def test_activity_skips_attach_screenshot_when_client_lacks_method(caplog):
    client = MagicMock(spec=["add_comment"])
    with patched_extraction(code_snippet="the code", annotation_text=None), \
         patch(f"{MODULE}.JiraClient", return_value=client):
        await capture_and_attach_screenshot_activity(make_input())

    client.add_comment.assert_called_once()
    assert "no attach_screenshot method" in caplog.text


async def test_activity_propagates_real_add_comment_error():
    client = MagicMock()
    client.add_comment.side_effect = RuntimeError("jira down")
    with patched_extraction(code_snippet="the code", annotation_text=None), \
         patch(f"{MODULE}.JiraClient", return_value=client):
        with pytest.raises(RuntimeError, match="jira down"):
            await capture_and_attach_screenshot_activity(make_input())


async def test_activity_skips_comment_when_extraction_empty():
    client = MagicMock()
    with patched_extraction(code_snippet=None, annotation_text=None), \
         patch(f"{MODULE}.JiraClient", return_value=client):
        await capture_and_attach_screenshot_activity(make_input())

    client.attach_screenshot.assert_called_once()
    client.add_comment.assert_not_called()


async def test_activity_cleans_up_temp_dir_on_failure():
    created = {}
    real_mkdtemp = tempfile_module.mkdtemp

    def spy_mkdtemp(*args, **kwargs):
        path = real_mkdtemp(*args, **kwargs)
        created["path"] = path
        return path

    with patch(f"{MODULE}.tempfile.mkdtemp", side_effect=spy_mkdtemp), \
         patch(f"{MODULE}.capture_issue_screenshot", AsyncMock(side_effect=RuntimeError("capture failed"))):
        with pytest.raises(RuntimeError, match="capture failed"):
            await capture_and_attach_screenshot_activity(make_input())

    assert not os.path.exists(created["path"])
