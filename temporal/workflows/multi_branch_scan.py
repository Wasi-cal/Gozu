# Copyright (c) 2026 Calfus Inc.
# Author: Wasiullah Rafeeq S
# Editor: Prakrit Mohanty
#
# Depends on: Temporal (Temporal Technologies) - workflow orchestration
# Depends on: temporal/workflows/scan_to_ticket.py - fans out into this workflow as a per-branch child

"""
Workflow for a multi-branch config's manual/direct scan (`gozu run`):
fans out into one ScanToTicketWorkflow child per branch, concurrently,
aggregating their created/skipped/deferred/closed results into one
combined TicketResult - cli/report.py's end-of-run summary always
renders exactly one report regardless of branch count, never one per
branch, because this is the only TicketResult it ever sees for a
multi-branch config.

Not used for webhook-triggered configs - SonarQube already delivers one
webhook per branch there, so there's nothing to fan out (see
receiver/app.py's branch-pattern check instead, which just gates which of
those per-branch deliveries proceed). This only matters for direct
invocation, where a single sonar-scanner run (and therefore a single
ceTaskId) covers whatever's actually checked out on the host - fanning
out here means each child independently fetches ITS OWN branch's findings
(fetch_findings_activity passes each child's `branch` through to the
scanner's query - see scanner/sonarqube_common.py), not re-running
sonar-scanner per branch. For a scanner that actually distinguishes
branches (SonarQube Cloud), children genuinely see different findings.
For one that doesn't (self-hosted Community Build), every child queries
the same underlying data, so dedupe (source-key-{finding_key}) is what
keeps that case from creating duplicate tickets - only the first child to
reach a given finding creates it, later children see it as
already-ticketed.
"""

import asyncio

from temporalio import workflow

from temporal.models.sonar_to_jira import SonarToJiraInput

with workflow.unsafe.imports_passed_through():
    from core.models import CreatedTicket, TicketResult
    from temporal.workflows.scan_to_ticket import ScanToTicketWorkflow


@workflow.defn
class MultiBranchScanWorkflow:
    @workflow.run
    async def run(self, input: SonarToJiraInput, branches: list[str]) -> TicketResult:
        workflow.logger.info(
            f"Fanning out to {len(branches)} branch(es) for project_key={input.project_key}: {branches}"
        )

        # workflow.info().workflow_id is this workflow's own ID (set by the
        # caller as "sonar-jira-{ceTaskId}") - appending the branch gives
        # each child a distinct, deterministic ID sharing that same
        # ceTaskId root, since all children really are processing one scan.
        parent_id = workflow.info().workflow_id
        results: list[TicketResult] = await asyncio.gather(
            *[
                workflow.execute_child_workflow(
                    ScanToTicketWorkflow.run,
                    input.model_copy(update={"branch": branch}),
                    id=f"{parent_id}-{branch.replace('/', '-')}",
                )
                for branch in branches
            ]
        )

        created: list[CreatedTicket] = [entry for result in results for entry in result.created]
        skipped: list[str] = [key for result in results for key in result.skipped]
        deferred: list[str] = [key for result in results for key in result.deferred]
        closed: list[str] = [key for result in results for key in result.closed]
        # Every child shares the same ticket destination in the common
        # case, so upsert_rollup_ticket() (create_tickets_activity) upserts
        # the exact same shared ticket per child rather than a distinct one
        # each - the first non-None one found is that shared key, not an
        # arbitrary pick among genuinely different tickets.
        rollup_ticket = next((result.rollup_ticket for result in results if result.rollup_ticket), None)
        workflow.logger.info(
            f"Multi-branch fan-out complete: {len(created)} ticket(s) created, {len(skipped)} skipped, "
            f"{len(deferred)} deferred, {len(closed)} auto-closed across {len(branches)} branch(es)"
        )
        return TicketResult(created=created, skipped=skipped, deferred=deferred, rollup_ticket=rollup_ticket, closed=closed)
