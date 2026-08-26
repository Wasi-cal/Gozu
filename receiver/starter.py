"""Starts SonarToJiraWorkflow in Temporal on behalf of the webhook route."""

from temporalio.client import Client

from temporal.data_converter import DATA_CONVERTER, TASK_QUEUE
from temporal.models.sonar_to_jira import SonarToJiraInput
from temporal.workflows.sonar_to_jira import SonarToJiraWorkflow

TEMPORAL_ADDRESS = "localhost:7233"


async def start_sonar_to_jira_workflow(project_key: str, task_id: str) -> str:
    """Start the workflow, returning its workflow ID."""
    client = await Client.connect(TEMPORAL_ADDRESS, data_converter=DATA_CONVERTER)
    workflow_id = f"sonar-to-jira-{project_key}-{task_id}"

    await client.start_workflow(
        SonarToJiraWorkflow.run,
        SonarToJiraInput(project_key=project_key, task_id=task_id),
        id=workflow_id,
        task_queue=TASK_QUEUE,
    )
    return workflow_id
