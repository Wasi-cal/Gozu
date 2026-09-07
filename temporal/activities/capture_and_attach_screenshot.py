# Copyright (c) 2026 Calfus Inc.
# Author: Prakrit Mohanty
# Editor: Wasiullah Rafeeq S
#
# Depends on: Temporal (Temporal Technologies) - workflow orchestration

"""Activity: render a syntax-highlighted source snippet for a finding and attach it to its ticket."""

import os
import shutil
import tempfile
from pathlib import Path

from temporalio import activity

from scanner.screenshot import render_finding_snippet
from temporal.models.screenshot_attach import ScreenshotAttachInput
from ticket.factory import build_ticket_client, get_ticket_client


@activity.defn
async def capture_and_attach_screenshot_activity(input: ScreenshotAttachInput) -> None:
    """
    Two independent best-effort pieces, not one all-or-nothing unit: the
    comment (finding.message - the same string the API already gave us
    when this finding was first fetched, nothing scraped) and the
    rendered-snippet attachment. A snippet-rendering failure is logged
    CLEARLY, naming the finding and the real reason, and the activity
    still returns normally - the ticket that's already been created
    stays created, just without the attachment, rather than a silent
    "nothing happened" with no attachment and no explanation either way.
    """
    finding = input.finding
    client = build_ticket_client(input.ticket_backend, input.credentials) if input.credentials else get_ticket_client()

    add_comment = getattr(client, "add_comment", None)
    if add_comment is not None:
        add_comment(input.ticket_key, finding.message)
    else:
        activity.logger.warning(f"{type(client).__name__} doesn't support add_comment; skipping comment for {input.ticket_key}")

    attach_screenshot = getattr(client, "attach_screenshot", None)
    if attach_screenshot is None:
        activity.logger.warning(
            f"{type(client).__name__} doesn't support attach_screenshot; skipping for {input.ticket_key}"
        )
        return

    token = input.credentials.get("sonar_token", os.environ.get("SONAR_TOKEN", ""))
    try:
        png_bytes = render_finding_snippet(finding, token)
    except Exception as e:
        activity.logger.warning(
            f"Snippet rendering failed for finding {finding.key} (ticket {input.ticket_key}): {e} - "
            "ticket already created, continuing without a snippet attachment"
        )
        return

    tmp_dir = tempfile.mkdtemp(prefix="finding-snippet-")
    try:
        snippet_path = Path(tmp_dir) / f"{finding.source_tool}-{finding.key}.png"
        snippet_path.write_bytes(png_bytes)
        attach_screenshot(input.ticket_key, snippet_path)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
