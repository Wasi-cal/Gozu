"""Workflow orchestrating the Sonar -> Jira pipeline."""

import asyncio
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

from temporal.models.sonar_to_jira import SonarToJiraInput

with workflow.unsafe.imports_passed_through():
    from temporal.activities.capture_and_attach_screenshot import capture_and_attach_screenshot_activity
    from temporal.activities.create_jira_tickets import create_jira_tickets_activity
    from temporal.activities.fetch_vulnerabilities import fetch_vulnerabilities_activity
    from temporal.models.screenshot_attach import ScreenshotAttachInput
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

        if jira_result.created:
            issues_by_key = {issue.key: issue for issue in issues}

            screenshot_results = await asyncio.gather(
                *[
                    workflow.execute_activity(
                        capture_and_attach_screenshot_activity,
                        ScreenshotAttachInput(
                            sonar_issue=issues_by_key[entry.sonar_key],
                            jira_issue_key=entry.jira_key,
                        ),
                        start_to_close_timeout=timedelta(seconds=45),
                        retry_policy=RetryPolicy(maximum_attempts=2),
                    )
                    for entry in jira_result.created
                ],
                return_exceptions=True,
            )

            for entry, result in zip(jira_result.created, screenshot_results):
                if isinstance(result, BaseException):
                    workflow.logger.warning(
                        f"Screenshot capture/attach failed for {entry.jira_key} "
                        f"(sonar issue {entry.sonar_key}): {result}"
                    )
                else:
                    workflow.logger.info(f"  attached screenshot to {entry.jira_key}")

        return jira_result
