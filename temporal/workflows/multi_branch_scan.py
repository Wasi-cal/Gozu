"""
Workflow for a multi-branch config's manual/direct scan (`gozu run`):
fans out into one ScanToTicketWorkflow child per branch, concurrently,
aggregating their created/skipped results into one summary.

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
        workflow.logger.info(
            f"Multi-branch fan-out complete: {len(created)} ticket(s) created, {len(skipped)} skipped "
            f"across {len(branches)} branch(es)"
        )
        return TicketResult(created=created, skipped=skipped)
