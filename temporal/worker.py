# Copyright (c) 2026 Calfus Inc.
# Author: Wasiullah Rafeeq S
# Editor: Prakrit Mohanty, Claude
#
# Depends on: Temporal (Temporal Technologies) - workflow orchestration
# Depends on: temporal/data_converter.py - Pydantic-aware data converter required to serialize workflow/activity payloads

import asyncio
import logging
import os

from temporalio.client import Client
from temporalio.worker import Worker

from core.constants import CONTACT_EMAILS
from temporal.activities.capture_and_attach_screenshot import (
    capture_and_attach_screenshot_activity,
)
from temporal.activities.create_tickets import create_tickets_activity
from temporal.activities.fetch_findings import fetch_findings_activity
from temporal.activities.reconcile_resolved_findings import (
    reconcile_resolved_findings_activity,
)
from temporal.data_converter import DATA_CONVERTER, TASK_QUEUE
from temporal.workflows.multi_branch_scan import MultiBranchScanWorkflow
from temporal.workflows.reconcile_only import ReconcileOnlyWorkflow
from temporal.workflows.scan_to_ticket import ScanToTicketWorkflow

logger = logging.getLogger(__name__)


class _ContactEmailsLogFilter(logging.Filter):
    """
    Appends CONTACT_EMAILS, as plain text, to every ERROR-level+ record
    this process logs - workflow/activity failures included, since
    temporalio's own internal loggers propagate up to this root handler
    too. No terminal is attached here for a clickable mailto: link (see
    cli/crash_handler.py's CLI-side equivalent) to make sense of - an ops
    person reading these logs after the fact is the audience instead.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno >= logging.ERROR:
            record.msg = f"{record.msg} (contact: {', '.join(CONTACT_EMAILS)})"
        return True


async def main():
    logging.basicConfig(level=logging.INFO)
    for handler in logging.getLogger().handlers:
        handler.addFilter(_ContactEmailsLogFilter())

    temporal_host = os.environ.get("TEMPORAL_HOST", "localhost:7233")
    client = await Client.connect(temporal_host, data_converter=DATA_CONVERTER)

    worker = Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[ScanToTicketWorkflow, MultiBranchScanWorkflow, ReconcileOnlyWorkflow],
        activities=[
            fetch_findings_activity,
            create_tickets_activity,
            capture_and_attach_screenshot_activity,
            reconcile_resolved_findings_activity,
        ],
    )

    print(f"Worker started, listening on task queue '{TASK_QUEUE}'...")
    try:
        await worker.run()
    except Exception:
        logger.exception("Worker crashed")
        raise


if __name__ == "__main__":
    asyncio.run(main())
