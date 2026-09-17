# Copyright (c) 2026 Calfus Inc.
# Author: Wasiullah Rafeeq S
# Editor: Prakrit Mohanty
#
# Depends on: src/config/store.py - persisting last-scan git state back to the config
# Depends on: src/cli/prerequisites/__init__.py - ensuring Java/sonar-scanner/trivy/semgrep are installed before scanning
# Depends on: src/scanner/trivy_client.py - runs the host-side trivy fs scan directly
# Depends on: src/cli/scan_runner/semgrep_exec.py - runs the host-side local semgrep scan directly

"""
`gozu run`'s actual logic: pick a config, run the config's scanner, wait for
any server-side processing to finish, then trigger the Temporal workflow
directly - no webhook involved.
"""

import asyncio

import config.store as config_store
from cli.prerequisites import ensure_java, ensure_semgrep, ensure_sonar_scanner, ensure_trivy
from cli.scan_runner.config_fields import config_branches, scanner_host_url
from cli.scan_runner.config_select import select_config
from cli.scan_runner.scanner_exec import (
    detect_git_branch,
    git_state,
    read_ce_task_id,
    run_scanner,
    wait_for_analysis,
)
from cli.scan_runner.semgrep_exec import run_semgrep_local_scan
from cli.scan_runner.workflow_trigger import trigger_reconcile_only, trigger_workflow
from cli.status import waiting, warning
from core.models import TicketResult
from scanner.trivy_client import TrivyClient

__all__ = ["run_scan_cycle", "select_config"]


def run_scan_cycle(
    config: dict, path: str, skip_unchanged: bool = False, ticket_cap: int | None = None
) -> tuple[str, list[str], TicketResult]:
    """One full scan -> ticket cycle: ensure prerequisites, run the config's
    scanner, then trigger the workflow. Returns the task id, branch(es)
    scanned, and the TicketResult for src/cli/report.py.

    `skip_unchanged`: when HEAD SHA and working-tree state exactly match
    the config's last successful scan (persisted in Postgres, surviving
    across invocations), the scan/fetch/create-tickets sequence is skipped
    entirely - auto-close reconciliation still runs every time, since a
    human can resolve a finding directly in the scanner's own UI.

    `ticket_cap`: a one-off override for this invocation only, resolved
    here (CLI flag wins, else the config's persisted value) and never
    written back to the config.
    """
    effective_ticket_cap = ticket_cap if ticket_cap is not None else config.get("ticket_cap")

    # Captured before the scanner runs and reused below - sonar-scanner
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

    if config["scanner_type"] == "trivy":
        ce_task_id, scanned_branches, ticket_result = _run_trivy_scan_cycle(config, path, effective_ticket_cap)
    elif config["scanner_type"] == "semgrep":
        ce_task_id, scanned_branches, ticket_result = _run_semgrep_scan_cycle(config, path, effective_ticket_cap)
    else:
        ce_task_id, scanned_branches, ticket_result = _run_sonarqube_scan_cycle(config, path, effective_ticket_cap)

    if skip_unchanged and pre_scan_git_state is not None:
        sha, dirty = pre_scan_git_state
        config_store.update_config_fields(config["name"], last_scan_sha=sha, last_scan_dirty=dirty)
        # Mutated in place too - a --watch loop reuses this same dict across
        # iterations, so the next cycle's comparison must see this immediately.
        config["last_scan_sha"] = sha
        config["last_scan_dirty"] = dirty

    return ce_task_id, scanned_branches, ticket_result


def _run_sonarqube_scan_cycle(
    config: dict, path: str, effective_ticket_cap: int | None
) -> tuple[str, list[str], TicketResult]:
    ensure_java()
    ensure_sonar_scanner()

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

    # Mirrors trigger_workflow()'s own branches>1 check exactly, to avoid
    # re-deciding it separately and risking drift.
    branches = config_branches(config)
    scanned_branches = branches if len(branches) > 1 else ([branch] if branch else [])

    return ce_task_id, scanned_branches, ticket_result


def _run_trivy_scan_cycle(
    config: dict, path: str, effective_ticket_cap: int | None
) -> tuple[str, list[str], TicketResult]:
    """
    Trivy's scan is a single synchronous subprocess call, run HOST-SIDE in
    this process - unlike SonarQube, there's no server-side analysis to
    wait for and no ceTaskId. Findings are computed here and handed
    directly into the workflow (SonarToJiraInput.pre_fetched_findings),
    since the Temporal worker container has no access to `path` itself.
    No branch concept applies to Trivy - only display_branch is set, for
    ticket labeling.

    A transient trivy fs failure here is not retried by Temporal (this
    runs entirely before trigger_workflow() is called) - the same
    trade-off _run_sonarqube_scan_cycle() already has for run_scanner().
    """
    ensure_trivy()

    display_branch = detect_git_branch(path)

    waiting(f"Running trivy fs against {path} ...")
    findings = TrivyClient().fetch_findings(path)
    waiting(f"trivy fs finished - {len(findings)} finding(s); triggering ScanToTicketWorkflow ...")

    ticket_result = asyncio.run(
        trigger_workflow(
            config,
            ce_task_id=None,
            branch=None,
            display_branch=display_branch,
            ticket_cap=effective_ticket_cap,
            pre_fetched_findings=findings,
        )
    )

    return f"(trivy fs - {len(findings)} finding(s))", [], ticket_result


def _run_semgrep_scan_cycle(
    config: dict, path: str, effective_ticket_cap: int | None
) -> tuple[str, list[str], TicketResult]:
    """
    Local Semgrep's scan is a single synchronous subprocess call, run
    HOST-SIDE in this process - same "no ceTaskId, no server-side wait"
    shape as Trivy. Unlike Trivy, Semgrep findings DO carry a real git
    branch (this scans an actual checkout) - that's stamped by
    fetch_findings_activity, not here, since pre_fetched_findings still
    routes through that activity for Semgrep (see workflow_trigger.py's
    trigger_workflow() docstring for why Trivy and Semgrep differ here).
    """
    ensure_semgrep()

    display_branch = detect_git_branch(path)

    waiting(f"Running semgrep against {path} ...")
    findings = run_semgrep_local_scan(path, display_branch)
    waiting(f"semgrep finished - {len(findings)} finding(s); triggering ScanToTicketWorkflow ...")

    ticket_result = asyncio.run(
        trigger_workflow(
            config,
            ce_task_id=None,
            branch=None,
            display_branch=display_branch,
            ticket_cap=effective_ticket_cap,
            pre_fetched_findings=findings,
        )
    )

    return f"(semgrep - {len(findings)} finding(s))", [], ticket_result
