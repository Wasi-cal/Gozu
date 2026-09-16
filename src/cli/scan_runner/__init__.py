# Copyright (c) 2026 Calfus Inc.
# Author: Wasiullah Rafeeq S
# Editor: Prakrit Mohanty
#
# Depends on: src/config/store.py - persisting last-scan git state back to the config
# Depends on: src/cli/prerequisites/__init__.py - ensuring Java/sonar-scanner are installed before scanning

"""
`gozu run`'s actual logic: pick a config, run sonar-scanner, wait for
SonarQube's server-side processing to finish, then trigger the Temporal
workflow directly - no webhook involved.
"""

import asyncio

import config.store as config_store
from cli.prerequisites import ensure_java, ensure_sonar_scanner
from cli.scan_runner.config_fields import config_branches, scanner_host_url
from cli.scan_runner.config_select import select_config
from cli.scan_runner.scanner_exec import (
    detect_git_branch,
    git_state,
    read_ce_task_id,
    run_scanner,
    wait_for_analysis,
)
from cli.scan_runner.workflow_trigger import trigger_reconcile_only, trigger_workflow
from cli.status import waiting, warning
from core.models import TicketResult

__all__ = ["run_scan_cycle", "select_config"]


def run_scan_cycle(
    config: dict, path: str, skip_unchanged: bool = False, ticket_cap: int | None = None
) -> tuple[str, list[str], TicketResult]:
    """One full scan -> ticket cycle: ensure prerequisites, run sonar-scanner,
    wait for server-side analysis, then trigger the workflow. Returns the
    task id, branch(es) scanned, and the TicketResult for src/cli/report.py.

    `skip_unchanged`: when HEAD SHA and working-tree state exactly match
    the config's last successful scan (persisted in Postgres, surviving
    across invocations), the scan/fetch/create-tickets sequence is skipped
    entirely - auto-close reconciliation still runs every time, since a
    human can resolve a finding directly in SonarQube's UI.

    `ticket_cap`: a one-off override for this invocation only, resolved
    here (CLI flag wins, else the config's persisted value) and never
    written back to the config.
    """
    ensure_java()
    ensure_sonar_scanner()

    effective_ticket_cap = ticket_cap if ticket_cap is not None else config.get("ticket_cap")

    # Captured before sonar-scanner runs and reused below - sonar-scanner
    # leaves .scannerwork/ behind, which would make git report "dirty" (and
    # --skip-unchanged permanently useless) if state were captured any later.
    pre_scan_git_state = git_state(path) if skip_unchanged else None

    if skip_unchanged and pre_scan_git_state is not None:
        current_sha, current_dirty = pre_scan_git_state
        if (
            not current_dirty
            and config.get("last_scan_dirty") is False
            and current_sha == config.get("last_scan_sha")
        ):
            warning(
                f"Skipping scan - no code changes since the last successful run "
                f"(HEAD {current_sha[:8]}, working tree clean) - reconciliation still runs"
            )
            closed = asyncio.run(trigger_reconcile_only(config))
            return "(skipped - no code changes)", [], TicketResult(closed=closed)

    # Only tag/query by branch for Premium - self-hosted Community Build
    # rejects sonar.branch.name outright, and Free rejects querying anything
    # but "main" at the API level.
    branch = detect_git_branch(path) if config.get("sonar_plan") == "premium" else None

    # Ticket labeling has no backend restriction, so this is detected for
    # every plan - without it, local/Free tickets always showed "Branch: unknown".
    display_branch = detect_git_branch(path)

    waiting(f"Running sonar-scanner against {path} ...")
    run_scanner(config, path, branch)

    ce_task_id = read_ce_task_id(path)
    waiting(f"sonar-scanner finished; waiting for SonarQube analysis task {ce_task_id} ...")

    wait_for_analysis(scanner_host_url(config), config["credentials"]["sonar_token"], ce_task_id)
    waiting("Analysis finished - triggering ScanToTicketWorkflow ...")

    ticket_result = asyncio.run(trigger_workflow(config, ce_task_id, branch, display_branch, effective_ticket_cap))

    if skip_unchanged and pre_scan_git_state is not None:
        sha, dirty = pre_scan_git_state
        config_store.update_config_fields(config["name"], last_scan_sha=sha, last_scan_dirty=dirty)
        # Mutated in place too - a --watch loop reuses this same dict across
        # iterations, so the next cycle's comparison must see this immediately.
        config["last_scan_sha"] = sha
        config["last_scan_dirty"] = dirty

    # Mirrors trigger_workflow()'s own branches>1 check exactly, to avoid
    # re-deciding it separately and risking drift.
    branches = config_branches(config)
    scanned_branches = branches if len(branches) > 1 else ([branch] if branch else [])

    return ce_task_id, scanned_branches, ticket_result
