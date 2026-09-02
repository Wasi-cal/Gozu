"""Starts ScanToTicketWorkflow in Temporal on behalf of the webhook route."""

from temporalio.client import Client

from temporal.data_converter import DATA_CONVERTER, TASK_QUEUE
from temporal.models.sonar_to_jira import SonarToJiraInput
from temporal.workflows.scan_to_ticket import ScanToTicketWorkflow

TEMPORAL_ADDRESS = "localhost:7233"


async def start_scan_to_ticket_workflow(project_key: str, task_id: str) -> str:
    """Start the workflow, returning its workflow ID."""
    client = await Client.connect(TEMPORAL_ADDRESS, data_converter=DATA_CONVERTER)
    workflow_id = f"sonar-to-jira-{project_key}-{task_id}"

    await client.start_workflow(
        ScanToTicketWorkflow.run,
        SonarToJiraInput(project_key=project_key, task_id=task_id),
        id=workflow_id,
        task_queue=TASK_QUEUE,
    )
    return workflow_id
