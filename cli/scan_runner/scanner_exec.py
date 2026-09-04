"""Running sonar-scanner itself and reading its result off disk/git."""

import subprocess
import time
from pathlib import Path

import requests

from cli.prerequisites import ensure_sonar_scanner, java_env
from cli.scan_runner.config_fields import require_project_key, scanner_host_url

_POLL_INTERVAL_SECONDS = 2
_POLL_TIMEOUT_SECONDS = 300
_TERMINAL_TASK_STATUSES = {"SUCCESS", "FAILED", "CANCELED"}


def _build_scanner_command(config: dict, path: str, branch: str | None) -> list[str]:
    scanner_bin = ensure_sonar_scanner()
    credentials = config["credentials"]
    project_key = require_project_key(config)
    host_url = scanner_host_url(config)

    command = [
        str(scanner_bin),
        f"-Dsonar.host.url={host_url}",
        f"-Dsonar.token={credentials['sonar_token']}",
        f"-Dsonar.projectKey={project_key}",
        f"-Dsonar.sources={path}",
    ]
    if config["scanner_mode"] == "cloud":
        command.append(f"-Dsonar.organization={credentials['sonar_organization']}")
        # sonar.branch.name is a Developer Edition+ feature on self-hosted
        # SonarQube (Community Build rejects it outright - confirmed live:
        # "Validation of project failed: ... Developer Edition or above is
        # required"), but SonarQube Cloud supports it on every plan
        # including Free. Only tagging it here for cloud keeps the scan's
        # own recorded branch consistent with what fetch_findings() later
        # queries by (scanner/sonarqube_common.py) - untagged, Community
        # Build always stores everything under its own hardcoded "main"
        # regardless of what's actually checked out on the host.
        if branch:
            command.append(f"-Dsonar.branch.name={branch}")
    return command


def run_scanner(config: dict, path: str, branch: str | None) -> None:
    command = _build_scanner_command(config, path, branch)
    result = subprocess.run(command, cwd=path, capture_output=True, text=True, env=java_env(), check=False)
    if result.returncode != 0:
        raise RuntimeError(
            f"sonar-scanner exited with status {result.returncode}.\n"
            f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
        )


def read_ce_task_id(path: str) -> str:
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


def wait_for_analysis(host_url: str, token: str, ce_task_id: str) -> None:
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


def detect_git_branch(path: str) -> str | None:
    """
    The branch actually checked out at `path` on the *host* - the worker
    container has no .git at all (excluded via .dockerignore), so this has
    to happen here, where a real checkout exists, and be passed through
    rather than asking the worker to introspect its own filesystem.
    """
    result = subprocess.run(
        ["git", "-C", path, "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None
