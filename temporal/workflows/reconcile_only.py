# Copyright (c) 2026 Calfus Inc.
# Author: Wasiullah Rafeeq S
# Editor: Claude
#
# Depends on: Temporal (Temporal Technologies) - workflow orchestration
# Depends on: temporal/activities/reconcile_resolved_findings.py - executes this activity to auto-close resolved findings

"""
Standalone reconciliation - no fetch-findings/create-tickets pipeline
around it. Used by `gozu run --skip-unchanged` (cli/scan_runner/__init__.py)
when the scan/fetch/create-tickets sequence is being skipped this cycle
because the working tree hasn't changed since the last successful scan:
a human can still resolve a finding directly in SonarQube's own UI with
zero code changes, so auto-close reconciliation must keep running every
cycle regardless of whether a new scan actually happened.

Same retry policy as ScanToTicketWorkflow's inline reconciliation step
(temporal/workflows/scan_to_ticket.py) - kept identical deliberately,
not because they share code, since duplicating one small RetryPolicy
here is simpler than factoring out a shared constant for a single value
used in exactly two places.
"""

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

from temporal.models.reconcile_resolved_findings import ReconcileResolvedFindingsInput

with workflow.unsafe.imports_passed_through():
    from temporal.activities.reconcile_resolved_findings import (
        reconcile_resolved_findings_activity,
    )


@workflow.defn
class ReconcileOnlyWorkflow:
    @workflow.run
    async def run(self, input: ReconcileResolvedFindingsInput) -> list[str]:
        closed_tickets = await workflow.execute_activity(
            reconcile_resolved_findings_activity,
            input,
            start_to_close_timeout=timedelta(seconds=60),
            retry_policy=RetryPolicy(
                maximum_attempts=3,
                non_retryable_error_types=["ScannerAuthError", "TicketAuthError"],
            ),
        )
        workflow.logger.info(
            f"Reconciled resolved findings (skip-unchanged cycle): auto-closed {len(closed_tickets)} ticket(s)"
        )
        for ticket_key in closed_tickets:
            workflow.logger.info(f"  auto-closed {ticket_key}")
        return closed_tickets
