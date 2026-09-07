# Copyright (c) 2026 Calfus Inc.
# Author: Wasiullah Rafeeq S
#
# Depends on: Temporal (Temporal Technologies) - workflow orchestration

"""Starting the Temporal workflow once SonarQube's analysis has finished."""

import os
import uuid

from temporalio.client import Client

from cli.scan_runner.config_fields import config_branches
from core.models import TicketResult
from temporal.data_converter import DATA_CONVERTER, TASK_QUEUE
from temporal.models.reconcile_resolved_findings import ReconcileResolvedFindingsInput
from temporal.models.sonar_to_jira import SonarToJiraInput
from temporal.workflows.multi_branch_scan import MultiBranchScanWorkflow
from temporal.workflows.reconcile_only import ReconcileOnlyWorkflow
from temporal.workflows.scan_to_ticket import ScanToTicketWorkflow


async def trigger_workflow(config: dict, ce_task_id: str, branch: str | None, ticket_cap: int | None = None) -> TicketResult:
    """
    Connects to the *host*-visible Temporal address (localhost:{TEMPORAL_PORT})
    - this runs on the host, not inside the Docker network, so it must NOT
    use the "temporal" hostname temporal/worker.py uses internally.

    Passes the selected config's scanner/ticket backend + credentials
    through, so fetch_findings_activity/create_tickets_activity/
    capture_and_attach_screenshot_activity build their clients from this
    specific config instead of falling back to the worker's own global
    env vars (which is what the webhook receiver's SonarToJiraInput -
    lacking these fields - still does).

    workflow_id is deterministic per SonarQube analysis (ceTaskId is unique
    per actual compute-engine task), the same idempotency pattern the old
    webhook receiver used with (project_key, webhook task_id).

    A multi-branch config (more than one entry in `branches`) starts
    MultiBranchScanWorkflow instead of a single ScanToTicketWorkflow - the
    trivial single-branch case (Free, or a single-branch Premium config)
    isn't wrapped in unnecessary fan-out.
    """
    temporal_port = os.environ.get("TEMPORAL_PORT", "7233")
    client = await Client.connect(f"localhost:{temporal_port}", data_converter=DATA_CONVERTER)

    workflow_input = SonarToJiraInput(
        project_key=config["project_key"],
        task_id=ce_task_id,
        scanner_type=config["scanner_type"],
        scanner_mode=config["scanner_mode"],
        ticket_backend=config["ticket_backend"],
        credentials=config["credentials"],
        branch=branch,
        ticket_cap=ticket_cap,
    )
    workflow_id = f"sonar-jira-{ce_task_id}"

    branches = config_branches(config)
    if len(branches) > 1:
        return await client.execute_workflow(
            MultiBranchScanWorkflow.run, args=[workflow_input, branches], id=workflow_id, task_queue=TASK_QUEUE
        )

    return await client.execute_workflow(ScanToTicketWorkflow.run, workflow_input, id=workflow_id, task_queue=TASK_QUEUE)


async def trigger_reconcile_only(config: dict) -> list[str]:
    """
    Runs ONLY reconciliation (ReconcileOnlyWorkflow) - used by
    `gozu run --skip-unchanged` when the scan/fetch/create-tickets
    sequence itself is being skipped this cycle: a human could still have
    resolved a finding directly in SonarQube's own UI with zero code
    changes, so auto-close must never be skipped alongside the scan.

    No ceTaskId exists for this cycle (nothing was scanned), so
    workflow_id can't reuse trigger_workflow()'s per-analysis idempotency
    key - a random one is fine here, since each skipped cycle's
    reconciliation is its own independent action, not something that
    needs replay-safety tied to a specific SonarQube analysis.
    """
    temporal_port = os.environ.get("TEMPORAL_PORT", "7233")
    client = await Client.connect(f"localhost:{temporal_port}", data_converter=DATA_CONVERTER)

    reconcile_input = ReconcileResolvedFindingsInput(
        scanner_type=config["scanner_type"],
        scanner_mode=config["scanner_mode"],
        ticket_backend=config["ticket_backend"],
        credentials=config["credentials"],
    )
    workflow_id = f"sonar-jira-reconcile-{uuid.uuid4().hex}"
    return await client.execute_workflow(
        ReconcileOnlyWorkflow.run, reconcile_input, id=workflow_id, task_queue=TASK_QUEUE
    )
