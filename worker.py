import asyncio
import logging

from temporalio.client import Client
from temporalio.worker import Worker

from activities import create_jira_tickets_activity, fetch_vulnerabilities_activity
from workflows import SonarToJiraWorkflow

TASK_QUEUE = "sonar-jira-queue"


async def main():
    logging.basicConfig(level=logging.INFO)

    client = await Client.connect("localhost:7233")

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
