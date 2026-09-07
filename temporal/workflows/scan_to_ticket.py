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
            ),
            start_to_close_timeout=timedelta(seconds=30),
            # SonarQube's issues/hotspots search index can lag a few seconds
            # behind a compute-engine task's own SUCCESS status - a project
            # genuinely not existing (a permanent error, what
            # maximum_attempts is mainly guarding against - see
            # create_tickets_activity below) looks identical over the API to
            # "not indexed yet" (both 404 "Project not found"). More
            # attempts + a longer initial backoff than the default gives
            # that indexing lag room to resolve before giving up for real -
            # unchanged by non_retryable_error_types below, which only ever
            # stops retrying a ScannerAuthError (an invalid/expired token -
            # genuinely permanent, no amount of waiting fixes it), never a
            # 404 - that stays on the retryable path exactly as before.
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
            CreateTicketsInput(findings=findings, ticket_backend=input.ticket_backend, credentials=input.credentials),
            start_to_close_timeout=timedelta(seconds=30),
            # TicketAuthError (invalid/expired Jira token) and
            # TicketValidationError (a permanently malformed request - bad
            # project key, invalid issue type/field) never get fixed by
            # retrying; everything else (404s, 429s, 5xxs, timeouts) stays
            # on the normal retryable path, unchanged.
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

        # Alongside/after ticket creation, same run - not a separate
        # trigger, and always on for every config. Never allowed to fail
        # this workflow: closing tickets automatically is a bonus on top
        # of the create pipeline above, which already succeeded by this
        # point, same "don't let a bonus feature undo real work" rule as
        # jira_client.py's sprint assignment and rollup-ticket upsert.
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
                # ScannerAuthError (invalid/expired scanner token, from
                # fetch_resolutions -> the same scanner-search codepath
                # fetch_findings_activity uses) and TicketAuthError
                # (invalid/expired ticket-backend token) never get fixed by
                # retrying - everything else this activity can raise past
                # its own per-claim try/excepts (transient existence-check
                # or auto-close failures) already stays contained inside
                # the activity itself and never reaches this policy at
                # all. maximum_attempts=3 matches create_tickets_activity:
                # this is a bonus step wrapped in the try/except right
                # below, so it's never worth retrying as hard as
                # fetch_findings_activity's real pipeline step.
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

        # Attached after create_tickets_activity already returned, not
        # requested from it directly - reconciliation is a separate
        # activity that runs afterward, but the CLI's end-of-run report
        # (cli/report.py) wants one combined TicketResult, not two return
        # values threaded separately through MultiBranchScanWorkflow too.
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
                        # TicketAuthError/TicketValidationError from the
                        # attach_screenshot()/add_comment() calls this
                        # activity makes are permanent, same reasoning as
                        # create_tickets_activity - never fixed by
                        # retrying. Everything else that can propagate
                        # here (Playwright navigation/timeout failures -
                        # scanner/screenshot.py's page.goto()/page.screenshot()
                        # calls; the best-effort text extraction helpers
                        # already degrade to None rather than raising) is
                        # transient network/timing, so it stays on the
                        # normal retryable path. maximum_attempts stays at
                        # 2, unchanged - this is a best-effort bonus
                        # feature (asyncio.gather(return_exceptions=True)
                        # below), never worth retrying as hard as a real
                        # pipeline step.
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
