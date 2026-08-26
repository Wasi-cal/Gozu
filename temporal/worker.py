import asyncio
import logging

from temporalio.client import Client
from temporalio.worker import Worker

from temporal.activities.create_jira_tickets import create_jira_tickets_activity
from temporal.activities.fetch_vulnerabilities import fetch_vulnerabilities_activity
from temporal.data_converter import DATA_CONVERTER, TASK_QUEUE
from temporal.workflows.sonar_to_jira import SonarToJiraWorkflow


async def main():
    logging.basicConfig(level=logging.INFO)

    client = await Client.connect("localhost:7233", data_converter=DATA_CONVERTER)

    worker = Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[SonarToJiraWorkflow],
        activities=[fetch_vulnerabilities_activity, create_jira_tickets_activity],
    )

    print(f"Worker started, listening on task queue '{TASK_QUEUE}'...")
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
