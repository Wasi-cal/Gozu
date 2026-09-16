# Copyright (c) 2026 Calfus Inc.
# Author: Wasiullah Rafeeq S
#
# Depends on: Temporal (Temporal Technologies) - workflow orchestration
# Depends on: src/temporal/activities/fetch_findings.py - executes this activity to fetch findings
# Depends on: src/temporal/activities/create_tickets.py - executes this activity to create tickets
# Depends on: src/temporal/activities/reconcile_resolved_findings.py - executes this activity to auto-close resolved findings
# Depends on: src/temporal/activities/capture_and_attach_screenshot.py - executes this activity to attach a screenshot to each created ticket

"""Workflow orchestrating the scan -> ticket pipeline, generic over which src/scanner/ticket backend is behind it."""

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
    from temporal.activities.reconcile_resolved_findings import (
        reconcile_resolved_findings_activity,
    )
    from temporal.models.create_tickets import CreateTicketsInput
    from temporal.models.fetch_findings import FetchFindingsInput
    from temporal.models.reconcile_resolved_findings import ReconcileResolvedFindingsInput
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
                display_branch=input.display_branch,
            ),
            start_to_close_timeout=timedelta(seconds=30),
            # SonarQube's search index can lag behind a task's SUCCESS status,
            # so a real 404 and "not indexed yet" look identical - extra
            # attempts/backoff give indexing time to catch up.
            retry_policy=RetryPolicy(
                initial_interval=timedelta(seconds=2),
                maximum_attempts=8,
                non_retryable_error_types=["ScannerAuthError"],
            ),
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
            CreateTicketsInput(
                findings=findings,
                ticket_backend=input.ticket_backend,
                credentials=input.credentials,
                ticket_cap=input.ticket_cap,
            ),
            start_to_close_timeout=timedelta(seconds=30),
            # Auth/validation errors are permanent, not worth retrying.
            retry_policy=RetryPolicy(
                maximum_attempts=3,
                non_retryable_error_types=["TicketAuthError", "TicketValidationError"],
            ),
        )

        workflow.logger.info(
            f"Tickets: created {len(ticket_result.created)} ticket(s), "
            f"skipped {len(ticket_result.skipped)} already-ticketed finding(s), "
            f"deferred {len(ticket_result.deferred)} finding(s) to the backlog rollup"
        )
        for entry in ticket_result.created:
            workflow.logger.info(f"  created {entry.ticket_key} for finding {entry.finding_key}")
        for finding_key in ticket_result.skipped:
            workflow.logger.info(f"  skipped finding {finding_key} (ticket already exists)")

        # Best-effort bonus on top of the create pipeline above - never
        # allowed to fail this workflow, same as jira_client.py's sprint
        # assignment and rollup-ticket upsert.
        closed_tickets: list[str] = []
        try:
            closed_tickets = await workflow.execute_activity(
                reconcile_resolved_findings_activity,
                ReconcileResolvedFindingsInput(
                    scanner_type=input.scanner_type,
                    scanner_mode=input.scanner_mode,
                    ticket_backend=input.ticket_backend,
                    credentials=input.credentials,
                ),
                start_to_close_timeout=timedelta(seconds=60),
                # Auth errors are permanent; other failures stay contained
                # inside the activity's own per-claim try/excepts.
                retry_policy=RetryPolicy(
                    maximum_attempts=3,
                    non_retryable_error_types=["ScannerAuthError", "TicketAuthError"],
                ),
            )
            workflow.logger.info(f"Reconciled resolved findings: auto-closed {len(closed_tickets)} ticket(s)")
            for ticket_key in closed_tickets:
                workflow.logger.info(f"  auto-closed {ticket_key}")
        except Exception as e:
            workflow.logger.warning(f"Reconciling resolved findings failed, leaving existing tickets untouched: {e}")

        # Merge in here, not returned separately - src/cli/report.py wants one
        # combined TicketResult.
        ticket_result = ticket_result.model_copy(update={"closed": closed_tickets})

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
                        # Auth/validation errors are permanent. Snippet-render
                        # failures are caught inside the activity itself and
                        # never reach this policy - what's left is transient
                        # network/timing on the Jira calls.
                        retry_policy=RetryPolicy(
                            maximum_attempts=2,
                            non_retryable_error_types=["TicketAuthError", "TicketValidationError"],
                        ),
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
