import asyncio
import logging

from temporalio.client import Client
from temporalio.worker import Worker

from temporal.activities.create_tickets import create_tickets_activity
from temporal.activities.fetch_findings import fetch_findings_activity
from temporal.data_converter import DATA_CONVERTER, TASK_QUEUE
from temporal.workflows.scan_to_ticket import ScanToTicketWorkflow


async def main():
    logging.basicConfig(level=logging.INFO)

    client = await Client.connect("localhost:7233", data_converter=DATA_CONVERTER)

    worker = Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[ScanToTicketWorkflow],
        activities=[fetch_findings_activity, create_tickets_activity],
    )

    print(f"Worker started, listening on task queue '{TASK_QUEUE}'...")
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
