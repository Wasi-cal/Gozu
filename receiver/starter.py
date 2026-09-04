# Copyright (c) 2026 Calfus Inc.
# Author: Wasiullah Rafeeq S
#
# Depends on: Temporal (Temporal Technologies) - workflow orchestration

"""Starts ScanToTicketWorkflow in Temporal on behalf of the webhook route."""

import os

from temporalio.client import Client

from temporal.data_converter import DATA_CONVERTER, TASK_QUEUE
from temporal.models.sonar_to_jira import SonarToJiraInput
from temporal.workflows.scan_to_ticket import ScanToTicketWorkflow


async def start_scan_to_ticket_workflow(config: dict, task_id: str, branch: str | None) -> str:
    """
    Start the workflow for a specific config's webhook delivery, returning
    its workflow ID (fire-and-forget - the caller doesn't wait for the
    workflow to finish, only for it to have started).

    Connects to TEMPORAL_HOST, not a hardcoded address: this runs inside
    the receiver container, on the same Docker network as the worker
    (docker-compose.yml sets TEMPORAL_HOST=temporal:7233 there) - unlike
    the host-side CLI (cli/scan_runner.py), which must use
    "localhost:{TEMPORAL_PORT}" instead since it runs outside that network.
    """
    temporal_host = os.environ.get("TEMPORAL_HOST", "localhost:7233")
    client = await Client.connect(temporal_host, data_converter=DATA_CONVERTER)
    workflow_id = f"sonar-jira-{task_id}"

    await client.start_workflow(
        ScanToTicketWorkflow.run,
        SonarToJiraInput(
            project_key=config["project_key"],
            task_id=task_id,
            scanner_type=config["scanner_type"],
            scanner_mode=config["scanner_mode"],
            ticket_backend=config["ticket_backend"],
            credentials=config["credentials"],
            branch=branch,
        ),
        id=workflow_id,
        task_queue=TASK_QUEUE,
    )
    return workflow_id
