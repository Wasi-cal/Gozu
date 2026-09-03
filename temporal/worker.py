import asyncio
import logging
import os

from temporalio.client import Client
from temporalio.worker import Worker

from temporal.activities.capture_and_attach_screenshot import (
    capture_and_attach_screenshot_activity,
)
from temporal.activities.create_tickets import create_tickets_activity
from temporal.activities.fetch_findings import fetch_findings_activity
from temporal.data_converter import DATA_CONVERTER, TASK_QUEUE
from temporal.workflows.multi_branch_scan import MultiBranchScanWorkflow
from temporal.workflows.scan_to_ticket import ScanToTicketWorkflow


async def main():
    logging.basicConfig(level=logging.INFO)

    temporal_host = os.environ.get("TEMPORAL_HOST", "localhost:7233")
    client = await Client.connect(temporal_host, data_converter=DATA_CONVERTER)

    worker = Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[ScanToTicketWorkflow, MultiBranchScanWorkflow],
        activities=[
            fetch_findings_activity,
            create_tickets_activity,
            capture_and_attach_screenshot_activity,
        ],
    )

    print(f"Worker started, listening on task queue '{TASK_QUEUE}'...")
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
