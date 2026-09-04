"""Local (self-hosted) SonarQube credential collection for the init wizard."""

import os

import questionary
import typer

from cli.init_wizard.prompts import ask_or_exit, generate_or_prompt_secret, prompt_text


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


def collect_local_sonar() -> tuple[dict[str, str], str, str]:
    """
    Returns (credentials, trigger_mode, project_key). Local self-hosted
    SonarQube is the backend the webhook receiver (receiver/app.py) already
    exists for, so trigger_mode defaults to "webhook" here and a webhook
    secret is collected to match receiver/verify_signature.py's HMAC check.
    """
    default_host = f"http://localhost:{os.environ.get('SONARQUBE_PORT', '9000')}"
    host_url = prompt_text("SonarQube host URL:", default=default_host)
    token = prompt_text("SonarQube token:", field="sonar_token_local")
    project_key = prompt_project_key()
    webhook_secret = generate_or_prompt_secret("Webhook secret")
    credentials = {"sonar_host_url": host_url, "sonar_token": token, "webhook_secret": webhook_secret}
    return credentials, "webhook", project_key


def select_scanner_mode() -> str:
    answer = ask_or_exit(questionary.select("Local or Cloud?", choices=["Local", "Cloud"]))
    return "local" if answer == "Local" else "cloud"
