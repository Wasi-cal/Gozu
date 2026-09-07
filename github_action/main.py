# Copyright (c) 2026 Calfus Inc.
# Author: Prakrit Mohanty
#
# Depends on: SonarQube (SonarSource) - direct API client
# Depends on: Jira (Atlassian) - ticket creation

"""
Entrypoint for the standalone GitHub Action
(.github/workflows/sonar-to-jira-main.yml): reacts to a push to main by
waiting for SonarQube Cloud's Automatic Analysis to land, then creating
Jira tickets for new findings - no Temporal, no Postgres, no gozu config
store involved. Credentials come straight from GitHub Actions secrets via
the environment, reusing the same env-reading factories
(get_scanner_client/get_ticket_client) the webhook receiver's legacy path
already provides - see scanner/factory.py and ticket/factory.py.

Dedupe is Jira's label search (find_existing) only, no ticket_claims
ledger - this workflow triggers serially on push:branches:[main] with
concurrency.cancel-in-progress: false, so the race that ledger exists to
close barely applies here.

Runs before any Temporal workflow/activity context exists, same as
receiver/app.py and temporal/worker.py - uses plain logging, not
activity.logger/workflow.logger (see CLAUDE.md).
"""

import asyncio
import logging
import os
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from core.models import Finding
from scanner.factory import get_scanner_client
from scanner.screenshot import render_finding_snippet
from ticket.factory import get_ticket_client

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

_BRANCH = "main"


def _write_step_summary(text: str) -> None:
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    with open(summary_path, "a") as f:
        f.write(text)


async def _capture_and_attach(client, finding: Finding, ticket_key: str, sonar_token: str) -> None:
    """
    Mirrors temporal/activities/capture_and_attach_screenshot.py's
    activity body - no Temporal context here. Two independent
    best-effort pieces, not one all-or-nothing unit: the comment
    (finding.message, nothing scraped) and the rendered-snippet
    attachment - a snippet-rendering failure is logged clearly (which
    finding, why) and doesn't prevent the comment or fail this entrypoint.
    """
    add_comment = getattr(client, "add_comment", None)
    if add_comment is not None:
        add_comment(ticket_key, finding.message)
    else:
        logger.warning(f"{type(client).__name__} doesn't support add_comment; skipping comment for {ticket_key}")

    attach_screenshot = getattr(client, "attach_screenshot", None)
    if attach_screenshot is None:
        logger.warning(f"{type(client).__name__} doesn't support attach_screenshot; skipping for {ticket_key}")
        return

    try:
        png_bytes = render_finding_snippet(finding, sonar_token)
    except Exception as e:
        logger.warning(
            f"Snippet rendering failed for finding {finding.key} (ticket {ticket_key}): {e} - "
            "ticket already created, continuing without a snippet attachment"
        )
        return

    tmp_dir = tempfile.mkdtemp(prefix="finding-snippet-")
    try:
        snippet_path = Path(tmp_dir) / f"{finding.source_tool}-{finding.key}.png"
        snippet_path.write_bytes(png_bytes)
        attach_screenshot(ticket_key, snippet_path)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


async def _attach_all(client, created: list[tuple[Finding, str]], sonar_token: str) -> list[object]:
    return await asyncio.gather(
        *(_capture_and_attach(client, finding, ticket_key, sonar_token) for finding, ticket_key in created),
        return_exceptions=True,
    )


def main() -> None:
    project_key = os.environ["SONAR_PROJECT_KEY"]
    not_before = datetime.fromisoformat(os.environ["COMMIT_TIMESTAMP"])
    sonar_token = os.environ.get("SONAR_TOKEN", "")

    scanner_client = get_scanner_client()

    # wait_for_latest_analysis isn't part of the generic ScannerClient
    # interface - like TicketClient's attach_screenshot/add_comment, it's a
    # bonus capability only SonarQubeCloudClient provides, dispatched via
    # getattr rather than called directly. Unlike those two, it's not
    # actually optional for this entrypoint (which only ever targets Cloud
    # via SCANNER_TYPE=sonarqube-cloud) - a missing method here means a
    # real misconfiguration, so it's a hard failure, not a skip.
    wait_for_latest_analysis = getattr(scanner_client, "wait_for_latest_analysis", None)
    if wait_for_latest_analysis is None:
        logger.error(
            f"{type(scanner_client).__name__} doesn't support wait_for_latest_analysis - "
            "this entrypoint only supports SonarQube Cloud (SCANNER_TYPE=sonarqube-cloud)."
        )
        sys.exit(1)

    logger.info(f"Waiting for SonarQube Cloud's Automatic Analysis of '{project_key}' branch '{_BRANCH}'...")
    try:
        wait_for_latest_analysis(project_key, _BRANCH, not_before)
    except (TimeoutError, RuntimeError) as e:
        logger.error(f"Failed waiting for analysis: {e}")
        sys.exit(1)

    findings = scanner_client.fetch_findings(project_key, branch=_BRANCH)
    logger.info(f"Fetched {len(findings)} open finding(s) for '{project_key}'.")

    ticket_client = get_ticket_client()

    created: list[tuple[Finding, str]] = []
    skipped: list[str] = []

    for finding in findings:
        existing = ticket_client.find_existing(finding.key)
        if existing:
            skipped.append(finding.key)
            continue
        ticket_key = ticket_client.create_ticket(finding)
        created.append((finding, ticket_key))

    if created:
        results = asyncio.run(_attach_all(ticket_client, created, sonar_token))
        for (finding, ticket_key), result in zip(created, results):
            if isinstance(result, BaseException):
                logger.warning(f"Screenshot/comment step failed for {ticket_key} (finding {finding.key}): {result}")

    summary = (
        f"Created {len(created)} ticket(s), skipped {len(skipped)} already-ticketed finding(s) "
        f"for '{project_key}' branch '{_BRANCH}'."
    )
    logger.info(summary)

    summary_lines = ["## Sonar to Jira\n\n", summary + "\n"]
    for finding, ticket_key in created:
        summary_lines.append(f"- created `{ticket_key}` for finding `{finding.key}`\n")
    for finding_key in skipped:
        summary_lines.append(f"- skipped finding `{finding_key}` (ticket already exists)\n")
    _write_step_summary("".join(summary_lines))


if __name__ == "__main__":
    main()
