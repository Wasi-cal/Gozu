# Copyright (c) 2026 Calfus Inc.
# Author: Prakrit Mohanty
# Editor: Wasiullah Rafeeq S

import os
import tempfile as tempfile_module
from unittest.mock import MagicMock, patch

import pytest

from core.models import Finding, Severity
from temporal.activities.capture_and_attach_screenshot import (
    capture_and_attach_screenshot_activity,
)
from temporal.models.screenshot_attach import ScreenshotAttachInput

MODULE = "temporal.activities.capture_and_attach_screenshot"

_FAKE_PNG = b"\x89PNG\r\n\x1a\nfake-png-bytes"


@pytest.fixture(autouse=True)
def jira_env(monkeypatch):
    monkeypatch.setenv("JIRA_URL", "https://example.atlassian.net")
    monkeypatch.setenv("JIRA_EMAIL", "bot@example.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "token")
    monkeypatch.setenv("JIRA_PROJECT_KEY", "PROJ")


def make_input() -> ScreenshotAttachInput:
    return ScreenshotAttachInput(
        finding=Finding(
            key="ABC-1",
            title="Vulnerability [S1]: some message (file.py:1)",
            severity=Severity.HIGH,
            component="proj:file.py",
            line=1,
            message="some message",
            finding_type="vulnerability",
            deep_link="http://localhost:9090/project/issues?id=proj",
            source_tool="sonarqube",
        ),
        ticket_key="PROJ-1",
    )


def patched_render(png_bytes=_FAKE_PNG, side_effect=None):
    if side_effect is not None:
        return patch(f"{MODULE}.render_finding_snippet", side_effect=side_effect)
    return patch(f"{MODULE}.render_finding_snippet", return_value=png_bytes)


async def test_activity_posts_comment_with_finding_message_directly():
    """No more DOM-scraped annotation text - the comment is exactly finding.message."""
    client = MagicMock()
    with patched_render(), patch(f"{MODULE}.get_ticket_client", return_value=client):
        await capture_and_attach_screenshot_activity(make_input())

    client.add_comment.assert_called_once_with("PROJ-1", "some message")


async def test_activity_attaches_rendered_snippet():
    # The temp file is cleaned up (shutil.rmtree) before this function
    # returns, so its bytes must be captured DURING the attach_screenshot
    # call itself, not read back afterward.
    captured: dict[str, bytes] = {}

    def fake_attach(ticket_key, path):
        captured["ticket_key"] = ticket_key
        captured["bytes"] = path.read_bytes()

    client = MagicMock()
    client.attach_screenshot.side_effect = fake_attach
    with patched_render(), patch(f"{MODULE}.get_ticket_client", return_value=client):
        await capture_and_attach_screenshot_activity(make_input())

    client.attach_screenshot.assert_called_once()
    assert captured["ticket_key"] == "PROJ-1"
    assert captured["bytes"] == _FAKE_PNG


async def test_activity_skips_add_comment_when_client_lacks_method(caplog):
    client = MagicMock(spec=["attach_screenshot"])
    with patched_render(), patch(f"{MODULE}.get_ticket_client", return_value=client):
        await capture_and_attach_screenshot_activity(make_input())

    client.attach_screenshot.assert_called_once()
    assert "doesn't support add_comment" in caplog.text


async def test_activity_skips_attach_screenshot_when_client_lacks_method(caplog):
    client = MagicMock(spec=["add_comment"])
    render_mock = MagicMock()
    with patch(f"{MODULE}.render_finding_snippet", render_mock), patch(f"{MODULE}.get_ticket_client", return_value=client):
        await capture_and_attach_screenshot_activity(make_input())

    client.add_comment.assert_called_once()
    assert "doesn't support attach_screenshot" in caplog.text
    # Never even attempts to render when there's no attach_screenshot support at all.
    render_mock.assert_not_called()


async def test_activity_logs_clearly_and_continues_when_snippet_rendering_fails(caplog):
    """Item 9: visible failure, not silent - the activity must NOT raise, and the log must name the finding and the reason."""
    client = MagicMock()
    with patched_render(side_effect=RuntimeError("SonarQube returned 500")), patch(
        f"{MODULE}.get_ticket_client", return_value=client
    ):
        await capture_and_attach_screenshot_activity(make_input())  # must not raise

    assert "ABC-1" in caplog.text
    assert "SonarQube returned 500" in caplog.text
    client.attach_screenshot.assert_not_called()
    # The comment (independent of snippet rendering) still went out.
    client.add_comment.assert_called_once()


async def test_activity_propagates_real_add_comment_error():
    """An actual Jira-side failure (not a snippet-rendering failure) is a genuine activity failure, unchanged."""
    client = MagicMock()
    client.add_comment.side_effect = RuntimeError("jira down")
    with patched_render(), patch(f"{MODULE}.get_ticket_client", return_value=client), pytest.raises(
        RuntimeError, match="jira down"
    ):
        await capture_and_attach_screenshot_activity(make_input())


async def test_activity_cleans_up_temp_dir_after_successful_attach():
    created = {}
    real_mkdtemp = tempfile_module.mkdtemp

    def spy_mkdtemp(*args, **kwargs):
        path = real_mkdtemp(*args, **kwargs)
        created["path"] = path
        return path

    client = MagicMock()
    with patch(f"{MODULE}.tempfile.mkdtemp", side_effect=spy_mkdtemp), patched_render(), patch(
        f"{MODULE}.get_ticket_client", return_value=client
    ):
        await capture_and_attach_screenshot_activity(make_input())

    assert not os.path.exists(created["path"])
