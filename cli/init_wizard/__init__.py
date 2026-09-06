# Copyright (c) 2026 Calfus Inc.
# Author: Wasiullah Rafeeq S
# Editor: Prakrit Mohanty

"""
`gozu init` - the interactive setup wizard. Provisions .env, checks/
installs prerequisites, walks through scanner + ticket credential
selection, and seeds one row via config.store.create_config(). Ends at "a
named config exists in Postgres" - actually running a scan is
cli/scan_runner.py.
"""

from pathlib import Path

import questionary
import typer

import config.store as config_store
from cli.init_wizard.env_step import step_bootstrap_env, step_ensure_prerequisites
from cli.init_wizard.jira_step import collect_jira
from cli.init_wizard.prompts import ask_or_exit
from cli.init_wizard.sonar_cloud import collect_cloud_sonar
from cli.init_wizard.sonar_local import collect_local_sonar, select_scanner_mode
from cli.init_wizard.summary import print_summary
from cli.stack.cleanup import InterruptCleanup
from cli.stack.files import ensure_stack_files
from cli.stack.profiles import ensure_postgres_up, is_service_up
from cli.status import error, waiting
from scanner.base import SCANNER_REGISTRY


def _select_scanner() -> str:
    choices = list(SCANNER_REGISTRY.items())
    if len(choices) == 1:
        scanner_type, label = choices[0]
        typer.echo(f"Only one scanner is registered: {label}")
        return scanner_type

    return ask_or_exit(
        questionary.select(
            "Choose a scanner:",
            choices=[questionary.Choice(title=label, value=scanner_type) for scanner_type, label in choices],
        )
    )


def _prompt_config_name() -> str:
    existing_names = {config["name"] for config in config_store.list_configs()}
    while True:
        name = ask_or_exit(questionary.text("Name this config:")).strip()
        if not name:
            error("Name can't be empty.")
            continue
        if name in existing_names:
            error(f"A config named '{name}' already exists - choose another name.")
            continue
        return name


def _run_init_wizard_body(stack_dir: Path, cleanup: InterruptCleanup) -> None:
    # Postgres has to come up right here, before `gozu up` is ever run,
    # because create_config() below needs a live connection to save this
    # config at all. Tracked BEFORE calling ensure_postgres_up(), not after
    # it returns - confirmed live that an interrupt landing while still
    # blocked waiting for postgres's own healthcheck (the single highest-
    # value moment to actually interrupt) never reaches a post-call
    # tracking line at all, so it would otherwise leave a container Docker
    # already created untracked and uncleaned-up. Pre-checking is_service_up()
    # first means only a genuinely-not-already-running postgres gets
    # tracked - the whole point of "only what this invocation itself
    # started fresh".
    if not is_service_up(stack_dir, "postgres"):
        cleanup.track("postgres")
    waiting("Bringing up Postgres ...")
    ensure_postgres_up(stack_dir, cleanup=cleanup)

    typer.secho("Step 2/3: scanner + credentials", bold=True)
    scanner_type = _select_scanner()
    scanner_mode = select_scanner_mode()

    # Both scanner_modes run sonar-scanner on THIS host (see
    # step_ensure_prerequisites()'s docstring) - unconditional, not gated
    # on Local vs Cloud.
    step_ensure_prerequisites()

    sonar_plan: str | None = None
    branches: str | None = None

    if scanner_mode == "local":
        credentials, trigger_mode, project_key = collect_local_sonar(stack_dir, cleanup)
    else:
        credentials, trigger_mode, project_key, sonar_plan, branches = collect_cloud_sonar()

    ticket_destination_id = collect_jira()

    typer.secho("Step 3/3: name this config", bold=True)
    name = _prompt_config_name()

    config_store.create_config(
        name=name,
        scanner_type=scanner_type,
        scanner_mode=scanner_mode,
        ticket_backend="jira",
        trigger_mode=trigger_mode,
        project_key=project_key,
        credentials=credentials,
        sonar_plan=sonar_plan,
        branches=branches,
        ticket_destination_id=ticket_destination_id,
    )

    destination = config_store.get_ticket_destination_by_id(ticket_destination_id)
    destination_name = destination["name"] if destination else None
    print_summary(name, scanner_type, scanner_mode, project_key, sonar_plan, branches, trigger_mode, destination_name)


def run_init_wizard() -> None:
    """
    `gozu init` can bring up Docker services during its own run (Postgres,
    always; SonarQube too, for a fresh Local config with nothing already
    running - see cli/init_wizard/sonar_local.py). Wrapped in
    InterruptCleanup (cli/stack/cleanup.py) so a Ctrl-C/SIGTERM partway
    through only tears down what THIS run itself started fresh, never a
    service that predates it.
    """
    typer.secho("gozu init", bold=True, underline=True)

    step_bootstrap_env()

    # Materializes docker-compose.yml/Dockerfile/sql/init.sql (+ the source
    # tree the worker/receiver images build from) into ~/.gozu/ - see
    # cli/stack/files.py. Pure file writes, no Docker interaction - safe to
    # do before the interrupt-cleanup scope even starts.
    stack_dir = ensure_stack_files()

    with InterruptCleanup(stack_dir) as cleanup:
        _run_init_wizard_body(stack_dir, cleanup)
