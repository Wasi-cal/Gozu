# Copyright (c) 2026 Calfus Inc.
# Author: Wasiullah Rafeeq S
# Editor: Prakrit Mohanty, Claude
#
# Depends on: cli/stack/cleanup.py - tracking SonarQube for interrupt cleanup if started fresh
# Depends on: cli/stack/profiles.py - starting gozu's managed SonarQube service

"""Local (self-hosted) SonarQube credential collection for the init wizard."""

import os
from pathlib import Path

import questionary
import requests

from cli.prompts import ask_or_exit, prompt_text
from cli.stack.cleanup import InterruptCleanup
from cli.stack.profiles import ensure_service_up
from cli.status import error, success, waiting, warning


def prompt_project_key(default: str = "") -> str:
    """
    Required for every new config: `gozu run` needs it for both the
    sonar-scanner command and the issues-fetch API call, and unlike the old
    webhook path there's no incoming payload to pull it from anymore.
    `default` lets `gozu config edit` prefill the current value.
    """
    while True:
        key = prompt_text("SonarQube project key:", field="sonar_project_key", default=default).strip()
        if key:
            return key
        error("Project key can't be empty.")


def _sonarqube_status(host_url: str) -> str | None:
    """SonarQube's own reported status ("UP", "STARTING", ...) at `host_url`, or None if nothing answers there."""
    try:
        response = requests.get(f"{host_url}/api/system/status", timeout=3)
        response.raise_for_status()
        return response.json().get("status")
    except requests.RequestException:
        return None


def _ensure_local_sonarqube(host_url: str, default_host: str, stack_dir: Path, cleanup: InterruptCleanup | None) -> bool:
    """
    Checks `host_url` before ever asking for a token - there's nothing to
    generate a token from if SonarQube isn't actually running yet. Starts
    gozu's own managed SonarQube (docker-compose.yml's sonarqube-local
    profile) if nothing answers at the default local address; anything
    already answering (gozu's own from a prior run, or an unrelated
    service on that port) gets a confirm instead of being silently
    assumed correct. Returns False if the caller should re-prompt for a
    different host URL instead of proceeding.

    `cleanup` (see cli/stack/cleanup.py) is told about SonarQube only if
    this call itself started it fresh - so a Ctrl-C partway through the
    rest of the wizard tears SonarQube back down again too, not just
    Postgres, but only when this run is the one that actually brought it up.
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
        warning(
            f"Nothing responding at {host_url} - gozu only manages its own SonarQube at {default_host}; "
            "start SonarQube at that address yourself, or enter gozu's own address instead."
        )
        return False

    # Tracked BEFORE calling ensure_service_up(), not after it returns -
    # SonarQube can take 30-60s+ to become healthy, so an interrupt landing
    # during that wait (the highest-value moment to interrupt) must still
    # result in cleanup, not just one landing after it's already healthy.
    if cleanup is not None:
        cleanup.track("sonarqube")
    waiting(f"No SonarQube found at {host_url} - starting it now (first run can take 30-60s+) ...")
    ensure_service_up(stack_dir, "sonarqube", profile="sonarqube-local", cleanup=cleanup)
    success(f"SonarQube is up at {host_url}.")
    return True


def ensure_local_sonarqube_host(stack_dir: Path, cleanup: InterruptCleanup | None = None) -> str:
    """
    The structural, one-shot part of Local setup: resolve+confirm a
    working SonarQube host URL, starting gozu's own managed instance if
    needed. Returns just the host_url - deliberately NOT wrapped as a
    cli/wizard_engine.py WizardField, since re-running this (with its real
    Docker-start side effect) from a review screen would be a much bigger
    operation than "correct a value" - this happens once, before the
    field list is even built, same as it always has.
    """
    default_host = f"http://localhost:{os.environ.get('SONARQUBE_PORT', '9000')}"
    while True:
        host_url = prompt_text("SonarQube host URL:", default=default_host)
        if _ensure_local_sonarqube(host_url, default_host, stack_dir, cleanup):
            return host_url


def prompt_sonar_token_local(default: str = "") -> str:
    return prompt_text("SonarQube token:", field="sonar_token_local", default=default)


def select_scanner_mode() -> str:
    answer = ask_or_exit(questionary.select("Local or Cloud?", choices=["Local", "Cloud"]))
    return "local" if answer == "Local" else "cloud"
