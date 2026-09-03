"""
`codescan run`'s actual logic: pick a config, run sonar-scanner, wait for
SonarQube's server-side processing to finish, then trigger the Temporal
workflow directly - no webhook involved.
"""

import asyncio

import typer

from cli.prerequisites import ensure_java, ensure_sonar_scanner
from cli.scan_runner.config_fields import scanner_host_url
from cli.scan_runner.config_select import select_config
from cli.scan_runner.scanner_exec import (
    detect_git_branch,
    read_ce_task_id,
    run_scanner,
    wait_for_analysis,
)
from cli.scan_runner.workflow_trigger import trigger_workflow

__all__ = ["run_scan_cycle", "select_config"]


def run_scan_cycle(config: dict, path: str) -> dict:
    """
    One full scan -> ticket cycle: ensure prerequisites, run sonar-scanner,
    wait for SonarQube's server-side analysis to actually finish, then
    trigger the workflow and wait for its result. Returns a plain summary
    dict for printing.
    """
    ensure_java()
    ensure_sonar_scanner()

    typer.echo(f"Running sonar-scanner against {path} ...")
    run_scanner(config, path)

    ce_task_id = read_ce_task_id(path)
    typer.echo(f"sonar-scanner finished; waiting for SonarQube analysis task {ce_task_id} ...")

    wait_for_analysis(scanner_host_url(config), config["credentials"]["sonar_token"], ce_task_id)
    typer.echo("Analysis finished - triggering ScanToTicketWorkflow ...")

    branch = detect_git_branch(path)
    ticket_result = asyncio.run(trigger_workflow(config, ce_task_id, branch))

    return {
        "ce_task_id": ce_task_id,
        "created": [{"finding_key": e.finding_key, "ticket_key": e.ticket_key} for e in ticket_result.created],
        "skipped": ticket_result.skipped,
    }
