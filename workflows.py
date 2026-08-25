"""Temporal workflow orchestrating the Sonar -> Jira pipeline."""

from datetime import timedelta

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from activities import create_jira_tickets_activity, fetch_vulnerabilities_activity


@workflow.defn
class SonarToJiraWorkflow:
    @workflow.run
    async def run(self, input: dict) -> dict:
        project_key = input["project_key"]
        task_id = input.get("task_id")

        workflow.logger.info(
            f"Starting SonarToJiraWorkflow for project_key={project_key} task_id={task_id}"
        )

        issues = await workflow.execute_activity(
            fetch_vulnerabilities_activity,
            project_key,
            start_to_close_timeout=timedelta(seconds=30),
        )

        workflow.logger.info(
            f"Fetched {len(issues)} vulnerabilities/hotspots for project_key={project_key}"
        )
        for issue in issues:
            workflow.logger.info(
                f"  [{issue['type']}] {issue['severity']} {issue['rule']} "
                f"{issue['component']}:{issue['line']} - {issue['message']} ({issue['deep_link']})"
            )

        jira_result = await workflow.execute_activity(
            create_jira_tickets_activity,
            issues,
            start_to_close_timeout=timedelta(seconds=30),
        )

        created = jira_result["created"]
        skipped = jira_result["skipped"]

        workflow.logger.info(
            f"Jira: created {len(created)} ticket(s), skipped {len(skipped)} already-ticketed issue(s)"
        )
        for entry in created:
            workflow.logger.info(f"  created {entry['jira_key']} for sonar issue {entry['sonar_key']}")
        for sonar_key in skipped:
            workflow.logger.info(f"  skipped sonar issue {sonar_key} (ticket already exists)")

        return jira_result
