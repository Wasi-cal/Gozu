"""
Workflow for a multi-branch config's manual/direct scan (`codescan run`):
fans out into one ScanToTicketWorkflow child per branch, concurrently,
aggregating their created/skipped results into one summary.

Not used for webhook-triggered configs - SonarQube already delivers one
webhook per branch there, so there's nothing to fan out (see
receiver/app.py's branch-pattern check instead, which just gates which of
those per-branch deliveries proceed). This only matters for direct
invocation, where a single sonar-scanner run (and therefore a single
ceTaskId) covers whatever's actually checked out on the host - fanning
out here means running the same fetched findings through the ticket
pipeline once per tracked branch (each child gets its own workflow_id and
stamps its own `branch` label on the resulting Finding objects), not
re-scanning per branch. Since all children share the same findings,
dedupe (source-key-{finding_key}) means only the first child to reach a
given finding actually creates its ticket - later children correctly see
it as already-ticketed and skip it, exactly like a normal rescan would.
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
