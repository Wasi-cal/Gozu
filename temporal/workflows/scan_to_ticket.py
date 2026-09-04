# Copyright (c) 2026 Calfus Inc.
# Author: Wasiullah Rafeeq S
#
# Depends on: Temporal (Temporal Technologies) - workflow orchestration

"""Workflow orchestrating the scan -> ticket pipeline, generic over which scanner/ticket backend is behind it."""

import asyncio
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

from temporal.models.sonar_to_jira import SonarToJiraInput

with workflow.unsafe.imports_passed_through():
    from core.models import TicketResult
    from temporal.activities.capture_and_attach_screenshot import (
        capture_and_attach_screenshot_activity,
    )
    from temporal.activities.create_tickets import create_tickets_activity
    from temporal.activities.fetch_findings import fetch_findings_activity
    from temporal.models.create_tickets import CreateTicketsInput
    from temporal.models.fetch_findings import FetchFindingsInput
    from temporal.models.screenshot_attach import ScreenshotAttachInput


@workflow.defn
class ScanToTicketWorkflow:
    @workflow.run
    async def run(self, input: SonarToJiraInput) -> TicketResult:
        workflow.logger.info(
            f"Starting ScanToTicketWorkflow for project_key={input.project_key} task_id={input.task_id}"
        )

        findings = await workflow.execute_activity(
            fetch_findings_activity,
            FetchFindingsInput(
                project_key=input.project_key,
                scanner_type=input.scanner_type,
                scanner_mode=input.scanner_mode,
                credentials=input.credentials,
                branch=input.branch,
            ),
            start_to_close_timeout=timedelta(seconds=30),
            # SonarQube's issues/hotspots search index can lag a few seconds
            # behind a compute-engine task's own SUCCESS status - a project
            # genuinely not existing (a permanent error, what
            # maximum_attempts is mainly guarding against - see
            # create_tickets_activity below) looks identical over the API to
            # "not indexed yet" (both 404 "Project not found"). More
            # attempts + a longer initial backoff than the default gives
            # that indexing lag room to resolve before giving up for real.
            retry_policy=RetryPolicy(initial_interval=timedelta(seconds=2), maximum_attempts=8),
        )

        workflow.logger.info(
            f"Fetched {len(findings)} findings for project_key={input.project_key}"
        )
        for finding in findings:
            workflow.logger.info(
                f"  [{finding.finding_type}] {finding.severity.value} "
                f"{finding.component}:{finding.line} - {finding.message} ({finding.deep_link})"
            )

        ticket_result = await workflow.execute_activity(
            create_tickets_activity,
            CreateTicketsInput(findings=findings, ticket_backend=input.ticket_backend, credentials=input.credentials),
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(maximum_attempts=3),
        )

        workflow.logger.info(
            f"Tickets: created {len(ticket_result.created)} ticket(s), "
            f"skipped {len(ticket_result.skipped)} already-ticketed finding(s)"
        )
        for entry in ticket_result.created:
            workflow.logger.info(f"  created {entry.ticket_key} for finding {entry.finding_key}")
        for finding_key in ticket_result.skipped:
            workflow.logger.info(f"  skipped finding {finding_key} (ticket already exists)")

        if ticket_result.created:
            findings_by_key = {finding.key: finding for finding in findings}

            screenshot_results = await asyncio.gather(
                *[
                    workflow.execute_activity(
                        capture_and_attach_screenshot_activity,
                        ScreenshotAttachInput(
                            finding=findings_by_key[entry.finding_key],
                            ticket_key=entry.ticket_key,
                            ticket_backend=input.ticket_backend,
                            credentials=input.credentials,
                        ),
                        start_to_close_timeout=timedelta(seconds=45),
                        retry_policy=RetryPolicy(maximum_attempts=2),
                    )
                    for entry in ticket_result.created
                ],
                return_exceptions=True,
            )

            for entry, result in zip(ticket_result.created, screenshot_results):
                if isinstance(result, BaseException):
                    workflow.logger.warning(
                        f"Screenshot capture/attach failed for {entry.ticket_key} "
                        f"(finding {entry.finding_key}): {result}"
                    )
                else:
                    workflow.logger.info(f"  attached screenshot to {entry.ticket_key}")

        return ticket_result
