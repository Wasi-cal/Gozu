# Copyright (c) 2026 Calfus Inc.
# Author: Wasiullah Rafeeq S
# Editor: Prakrit Mohanty
#
# Depends on: config/store.py - persisting last-scan git state back to the config
# Depends on: cli/prerequisites/__init__.py - ensuring Java/sonar-scanner are installed before scanning

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
    """
    One full scan -> ticket cycle: ensure prerequisites, run sonar-scanner,
    wait for SonarQube's server-side analysis to actually finish, then
    trigger the workflow and wait for its result. Returns the SonarQube
    compute-engine task id (or a placeholder string when `skip_unchanged`
    skipped the scan itself), the branch(es) actually scanned, and the
    (possibly multi-branch-aggregated) TicketResult - for cli/report.py's
    end-of-run summary to render.

    `skip_unchanged` (--skip-unchanged): when the scan path is a git repo
    whose HEAD SHA and clean/dirty working-tree state exactly match this
    config's last successful scan (config["last_scan_sha"]/
    ["last_scan_dirty"], persisted in Postgres - see
    migrations/versions/0007_add_last_scan_tracking.py - so this survives
    across separate `gozu run` invocations, not just one --watch loop),
    the scan/fetch/create-tickets sequence is skipped entirely. Auto-close
    reconciliation still runs every time regardless (trigger_reconcile_only()
    below) - a human can resolve a finding directly in SonarQube's own UI
    with zero code changes, so that must never be skipped alongside the
    scan. A non-git path, or one with no prior recorded state, always
    scans normally - exactly as if --skip-unchanged were never passed.

    `ticket_cap` (--ticket-cap/-t): a one-off override for THIS invocation
    only - resolved here (CLI flag wins if given, else the config's own
    persisted `ticket_cap`, migrations/versions/0008_add_ticket_cap.py)
    into a single effective value threaded through to
    create_tickets_activity. Never written back to the config - a
    genuinely separate knob from `gozu config edit`'s persistent default,
    even though they share the same fallback-to-BACKLOG_CAP-when-None
    behavior once resolved.
    """
    ensure_java()
    ensure_sonar_scanner()

    effective_ticket_cap = ticket_cap if ticket_cap is not None else config.get("ticket_cap")

    # Captured once, BEFORE sonar-scanner ever runs, and reused below for
    # the post-scan record too - not re-queried afterward. sonar-scanner
    # leaves .scannerwork/ behind as untracked cruft in `path`; confirmed
    # live that re-running `git status --porcelain` after the scan sees
    # that directory and reports "dirty" even for a genuinely
    # committed-and-clean tree, which would make --skip-unchanged
    # permanently useless (every recorded state ends up "dirty",
    # so no later run could ever match it) if state were captured any
    # later than this.
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
    # rejects sonar.branch.name outright (a Developer Edition+ feature), and
    # Free rejects querying anything but "main" at the API level ("Organization
    # is not allowed to access data from non main branches" - confirmed live)
    # even though it'll happily tag a scan with any branch name. Both
    # untagged/unscoped, a scan+fetch just uses whatever each backend treats
    # as its one implicit branch - see scanner_exec.py's _build_scanner_command().
    branch = detect_git_branch(path) if config.get("sonar_plan") == "premium" else None

    # Unlike `branch` above, ticket labeling has no backend restriction to
    # respect - SonarQube never needs to know or agree with this value for
    # it to be useful on a Jira ticket, so this is detected for every
    # scanner_mode/sonar_plan, not just Premium (see
    # SonarToJiraInput.display_branch's docstring). Confirmed live: without
    # this, every local/Free-plan ticket showed "Branch: unknown" even
    # when the scanned checkout was on a real, named branch.
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
        # Mutated in place, not just persisted to Postgres - a --watch
        # loop reuses this exact same dict across iterations (see
        # cli/main.py's run()), so the next cycle's comparison above
        # must see this update immediately too, not just a future
        # separate `gozu run` invocation re-reading it fresh from the DB.
        config["last_scan_sha"] = sha
        config["last_scan_dirty"] = dirty

    # Mirrors workflow_trigger.trigger_workflow()'s own branches>1 check
    # exactly (same config_branches() call) - so this always reflects
    # whichever branch(es) that call actually decided to fan out to,
    # rather than re-deciding it separately and risking drift.
    branches = config_branches(config)
    scanned_branches = branches if len(branches) > 1 else ([branch] if branch else [])

    return ce_task_id, scanned_branches, ticket_result
