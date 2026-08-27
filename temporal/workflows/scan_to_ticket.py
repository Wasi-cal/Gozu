"""Workflow orchestrating the scan -> ticket pipeline, generic over which scanner/ticket backend is behind it."""

from datetime import timedelta

from temporalio import workflow

from temporal.models.sonar_to_jira import SonarToJiraInput

with workflow.unsafe.imports_passed_through():
    from core.models import TicketResult
    from temporal.activities.create_tickets import create_tickets_activity
    from temporal.activities.fetch_findings import fetch_findings_activity


@workflow.defn
class ScanToTicketWorkflow:
    @workflow.run
    async def run(self, input: SonarToJiraInput) -> TicketResult:
        workflow.logger.info(
            f"Starting ScanToTicketWorkflow for project_key={input.project_key} task_id={input.task_id}"
        )

        findings = await workflow.execute_activity(
            fetch_findings_activity,
            input.project_key,
            start_to_close_timeout=timedelta(seconds=30),
        )

        workflow.logger.info(
            f"Fetched {len(findings)} findings for project_key={input.project_key}"
        )
        for finding in findings:
            workflow.logger.info(
                f"  [{finding['finding_type']}] {finding['severity']} "
                f"{finding['component']}:{finding['line']} - {finding['message']} ({finding['deep_link']})"
            )

        ticket_result = await workflow.execute_activity(
            create_tickets_activity,
            findings,
            start_to_close_timeout=timedelta(seconds=30),
        )

        workflow.logger.info(
            f"Tickets: created {len(ticket_result.created)} ticket(s), "
            f"skipped {len(ticket_result.skipped)} already-ticketed finding(s)"
        )
        for entry in ticket_result.created:
            workflow.logger.info(f"  created {entry.ticket_key} for finding {entry.finding_key}")
        for finding_key in ticket_result.skipped:
            workflow.logger.info(f"  skipped finding {finding_key} (ticket already exists)")

        return ticket_result
