"""Workflow orchestrating the Sonar -> Jira pipeline."""

from datetime import timedelta

from temporalio import workflow

from temporal.models.sonar_to_jira import SonarToJiraInput

with workflow.unsafe.imports_passed_through():
    from temporal.activities.create_jira_tickets import create_jira_tickets_activity
    from temporal.activities.fetch_vulnerabilities import fetch_vulnerabilities_activity
    from jira.models import JiraTicketResult


@workflow.defn
class SonarToJiraWorkflow:
    @workflow.run
    async def run(self, input: SonarToJiraInput) -> JiraTicketResult:
        workflow.logger.info(
            f"Starting SonarToJiraWorkflow for project_key={input.project_key} task_id={input.task_id}"
        )

        issues = await workflow.execute_activity(
            fetch_vulnerabilities_activity,
            input.project_key,
            start_to_close_timeout=timedelta(seconds=30),
        )

        workflow.logger.info(
            f"Fetched {len(issues)} vulnerabilities/hotspots for project_key={input.project_key}"
        )
        for issue in issues:
            workflow.logger.info(
                f"  [{issue.type}] {issue.severity} {issue.rule} "
                f"{issue.component}:{issue.line} - {issue.message} ({issue.deep_link})"
            )

        jira_result = await workflow.execute_activity(
            create_jira_tickets_activity,
            issues,
            start_to_close_timeout=timedelta(seconds=30),
        )

        workflow.logger.info(
            f"Jira: created {len(jira_result.created)} ticket(s), "
            f"skipped {len(jira_result.skipped)} already-ticketed issue(s)"
        )
        for entry in jira_result.created:
            workflow.logger.info(f"  created {entry.jira_key} for sonar issue {entry.sonar_key}")
        for sonar_key in jira_result.skipped:
            workflow.logger.info(f"  skipped sonar issue {sonar_key} (ticket already exists)")

        return jira_result
