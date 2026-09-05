# Copyright (c) 2026 Calfus Inc.
# Author: Wasiullah Rafeeq S
# Editor: Prakrit Mohanty

"""Local (self-hosted) SonarQube credential collection for the init wizard."""

import os
from pathlib import Path

import questionary
import requests
import typer

from cli.init_wizard.prompts import ask_or_exit, generate_or_prompt_secret, prompt_text
from cli.stack.profiles import ensure_service_up


def prompt_project_key() -> str:
    """
    Required for every new config: `gozu run` needs it for both the
    sonar-scanner command and the issues-fetch API call, and unlike the old
    webhook path there's no incoming payload to pull it from anymore.
    """
    while True:
        key = prompt_text("SonarQube project key:", field="sonar_project_key").strip()
        if key:
            return key
        typer.secho("Project key can't be empty.", fg=typer.colors.RED)


def _sonarqube_status(host_url: str) -> str | None:
    """SonarQube's own reported status ("UP", "STARTING", ...) at `host_url`, or None if nothing answers there."""
    try:
        response = requests.get(f"{host_url}/api/system/status", timeout=3)
        response.raise_for_status()
        return response.json().get("status")
    except requests.RequestException:
        return None


def _ensure_local_sonarqube(host_url: str, default_host: str, stack_dir: Path) -> bool:
    """
    Checks `host_url` before ever asking for a token - there's nothing to
    generate a token from if SonarQube isn't actually running yet. Starts
    gozu's own managed SonarQube (docker-compose.yml's sonarqube-local
    profile) if nothing answers at the default local address; anything
    already answering (gozu's own from a prior run, or an unrelated
    service on that port) gets a confirm instead of being silently
    assumed correct. Returns False if the caller should re-prompt for a
    different host URL instead of proceeding.
    """
    status = _sonarqube_status(host_url)

    if status == "UP":
        return ask_or_exit(questionary.confirm(f"SonarQube is already running at {host_url} - use it?", default=True))

    if status is not None:
        return ask_or_exit(
            questionary.confirm(
                f"Something's responding at {host_url}, but it doesn't look like a healthy SonarQube "
                f"(status: {status!r}) - use it anyway?",
                default=False,
            )
        )

    if host_url != default_host:
        typer.secho(
            f"Nothing responding at {host_url} - gozu only manages its own SonarQube at {default_host}; "
            "start SonarQube at that address yourself, or enter gozu's own address instead.",
            fg=typer.colors.YELLOW,
        )
        return False

    typer.echo(f"No SonarQube found at {host_url} - starting it now (first run can take 30-60s+) ...")
    ensure_service_up(stack_dir, "sonarqube", profile="sonarqube-local")
    typer.secho(f"SonarQube is up at {host_url}.", fg=typer.colors.GREEN)
    return True


def collect_local_sonar(stack_dir: Path) -> tuple[dict[str, str], str, str]:
    """
    Returns (credentials, trigger_mode, project_key). Local self-hosted
    SonarQube is the backend the webhook receiver (receiver/app.py) already
    exists for, so trigger_mode defaults to "webhook" here and a webhook
    secret is collected to match receiver/verify_signature.py's HMAC check.
    """
    default_host = f"http://localhost:{os.environ.get('SONARQUBE_PORT', '9000')}"
    while True:
        host_url = prompt_text("SonarQube host URL:", default=default_host)
        if _ensure_local_sonarqube(host_url, default_host, stack_dir):
            break

    token = prompt_text("SonarQube token:", field="sonar_token_local")
    project_key = prompt_project_key()
    webhook_secret = generate_or_prompt_secret("Webhook secret")
    credentials = {"sonar_host_url": host_url, "sonar_token": token, "webhook_secret": webhook_secret}
    return credentials, "webhook", project_key


def select_scanner_mode() -> str:
    answer = ask_or_exit(questionary.select("Local or Cloud?", choices=["Local", "Cloud"]))
    return "local" if answer == "Local" else "cloud"
