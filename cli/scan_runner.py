"""
`codescan run`'s actual logic: pick a config, run sonar-scanner, wait for
SonarQube's server-side processing to finish, then trigger
ScanToTicketWorkflow directly - no webhook involved. See
cli/init_wizard.py for how configs get created in the first place.
"""

import asyncio
import os
import subprocess
import time
from pathlib import Path

import questionary
import requests
import typer
from temporalio.client import Client

import config_store
from cli.prerequisites import ensure_java, ensure_sonar_scanner, java_env
from core.models import TicketResult
from temporal.data_converter import DATA_CONVERTER, TASK_QUEUE
from temporal.models.sonar_to_jira import SonarToJiraInput
from temporal.workflows.scan_to_ticket import ScanToTicketWorkflow

_POLL_INTERVAL_SECONDS = 2
_POLL_TIMEOUT_SECONDS = 300
_TERMINAL_TASK_STATUSES = {"SUCCESS", "FAILED", "CANCELED"}


def select_config(name: str | None) -> dict:
    """
    Resolve which config `codescan run` should use.

    A given `name` is looked up directly (clear error if it doesn't
    exist). With no name: errors if none exist ("run `codescan init`
    first"), auto-selects (and announces) the only one if exactly one
    exists, or shows an informed questionary select (name + scanner_mode +
    trigger_mode, not just a bare name list) if there are several.
    """
    if name:
        config = config_store.get_config(name)
        if config is None:
            typer.secho(f"No config named '{name}' found. Run `codescan init` to create one.", fg=typer.colors.RED)
            raise typer.Exit(code=1)
        return config

    configs = config_store.list_configs()
    if not configs:
        typer.secho("No configs found - run `codescan init` first.", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    if len(configs) == 1:
        chosen_name = configs[0]["name"]
        typer.echo(f"Auto-selected the only config: {chosen_name}")
    else:
        answer = questionary.select(
            "Which config?",
            choices=[
                questionary.Choice(
                    title=f"{c['name']}  ({c['scanner_mode']}, trigger={c['trigger_mode']})",
                    value=c["name"],
                )
                for c in configs
            ],
        ).ask()
        if answer is None:
            raise typer.Exit(code=1)
        chosen_name = answer

    config = config_store.get_config(chosen_name)
    if config is None:
        # Vanishingly unlikely (deleted between list_configs() and here), but
        # get_config() can genuinely return None, so this has to be handled.
        typer.secho(f"Config '{chosen_name}' disappeared before it could be loaded.", fg=typer.colors.RED)
        raise typer.Exit(code=1)
    return config


def _scanner_host_url(config: dict) -> str:
    """SonarQube Cloud is always sonarcloud.io; "local" uses whatever host URL the config was given."""
    return config["credentials"]["sonar_host_url"] if config["scanner_mode"] == "local" else "https://sonarcloud.io"


def _require_project_key(config: dict) -> str:
    project_key = config.get("project_key")
    if not project_key:
        raise RuntimeError(
            f"Config '{config['name']}' has no project_key (it predates that field) - "
            "recreate it with `codescan init` before running a scan."
        )
    return project_key


def _build_scanner_command(config: dict, path: str) -> list[str]:
    scanner_bin = ensure_sonar_scanner()
    credentials = config["credentials"]
    project_key = _require_project_key(config)
    host_url = _scanner_host_url(config)

    command = [
        str(scanner_bin),
        f"-Dsonar.host.url={host_url}",
        f"-Dsonar.token={credentials['sonar_token']}",
        f"-Dsonar.projectKey={project_key}",
        f"-Dsonar.sources={path}",
    ]
    if config["scanner_mode"] == "cloud":
        command.append(f"-Dsonar.organization={credentials['sonar_organization']}")

    return command


def _run_scanner(config: dict, path: str) -> None:
    command = _build_scanner_command(config, path)
    result = subprocess.run(command, cwd=path, capture_output=True, text=True, env=java_env(), check=False)
    if result.returncode != 0:
        raise RuntimeError(
            f"sonar-scanner exited with status {result.returncode}.\n"
            f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
        )


def _read_ce_task_id(path: str) -> str:
    """
    sonar-scanner exiting successfully only means the *client-side* upload
    finished - SonarQube processes the analysis asynchronously server-side,
    and report-task.txt's ceTaskId is what that server-side compute-engine
    task is tracked under. This is exactly what SonarQube's own webhook is
    signaling under the hood when it fires; skipping this step and treating
    "scanner subprocess exited" as "scan is done" would race the real
    result.
    """
    report_path = Path(path) / ".scannerwork" / "report-task.txt"
    if not report_path.is_file():
        raise RuntimeError(f"{report_path} doesn't exist - sonar-scanner may not have run successfully")

    for line in report_path.read_text().splitlines():
        key, _, value = line.partition("=")
        if key.strip() == "ceTaskId":
            return value.strip()

    raise RuntimeError(f"{report_path} has no ceTaskId field")


def _wait_for_analysis(host_url: str, token: str, ce_task_id: str) -> None:
    """Poll SonarQube's compute-engine task status until it reaches a terminal state."""
    deadline = time.monotonic() + _POLL_TIMEOUT_SECONDS
    url = f"{host_url.rstrip('/')}/api/ce/task"

    while True:
        response = requests.get(url, params={"id": ce_task_id}, auth=(token, ""), timeout=30)
        response.raise_for_status()
        task = response.json()["task"]
        status = task["status"]

        if status == "SUCCESS":
            return
        if status in _TERMINAL_TASK_STATUSES:
            error_message = task.get("errorMessage", "no error message provided")
            raise RuntimeError(f"SonarQube analysis task {ce_task_id} ended with status {status}: {error_message}")

        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"Timed out after {_POLL_TIMEOUT_SECONDS}s waiting for SonarQube analysis task "
                f"{ce_task_id} to finish (last status: {status})"
            )
        time.sleep(_POLL_INTERVAL_SECONDS)


def _detect_git_branch(path: str) -> str | None:
    """
    The branch actually checked out at `path` on the *host* - the worker
    container has no .git at all (excluded via .dockerignore), so this has
    to happen here, where a real checkout exists, and be passed through
    rather than asking the worker to introspect its own filesystem.
    """
    result = subprocess.run(
        ["git", "-C", path, "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


async def _trigger_workflow(config: dict, ce_task_id: str, branch: str | None) -> TicketResult:
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
    """
    temporal_port = os.environ.get("TEMPORAL_PORT", "7233")
    client = await Client.connect(f"localhost:{temporal_port}", data_converter=DATA_CONVERTER)

    return await client.execute_workflow(
        ScanToTicketWorkflow.run,
        SonarToJiraInput(
            project_key=config["project_key"],
            task_id=ce_task_id,
            scanner_type=config["scanner_type"],
            scanner_mode=config["scanner_mode"],
            ticket_backend=config["ticket_backend"],
            credentials=config["credentials"],
            branch=branch,
        ),
        id=f"sonar-jira-{ce_task_id}",
        task_queue=TASK_QUEUE,
    )


def run_scan_cycle(config: dict, path: str) -> dict:
    """
    One full scan -> ticket cycle: ensure prerequisites, run sonar-scanner,
    wait for SonarQube's server-side analysis to actually finish, then
    trigger ScanToTicketWorkflow and wait for its result. Returns a plain
    summary dict for printing (never fires the workflow and moves on
    without waiting for it).
    """
    ensure_java()
    ensure_sonar_scanner()

    typer.echo(f"Running sonar-scanner against {path} ...")
    _run_scanner(config, path)

    ce_task_id = _read_ce_task_id(path)
    typer.echo(f"sonar-scanner finished; waiting for SonarQube analysis task {ce_task_id} ...")

    _wait_for_analysis(_scanner_host_url(config), config["credentials"]["sonar_token"], ce_task_id)
    typer.echo("Analysis finished - triggering ScanToTicketWorkflow ...")

    branch = _detect_git_branch(path)
    ticket_result = asyncio.run(_trigger_workflow(config, ce_task_id, branch))

    return {
        "ce_task_id": ce_task_id,
        "created": [{"finding_key": e.finding_key, "ticket_key": e.ticket_key} for e in ticket_result.created],
        "skipped": ticket_result.skipped,
    }
