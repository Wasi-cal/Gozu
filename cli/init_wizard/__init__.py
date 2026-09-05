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

import questionary
import typer

import config.store as config_store
from cli.init_wizard.env_step import step_bootstrap_env, step_ensure_java
from cli.init_wizard.jira_step import collect_jira
from cli.init_wizard.prompts import ask_or_exit
from cli.init_wizard.sonar_cloud import collect_cloud_sonar
from cli.init_wizard.sonar_local import collect_local_sonar, select_scanner_mode
from cli.init_wizard.summary import print_summary
from cli.stack.files import ensure_stack_files
from cli.stack.profiles import ensure_postgres_up
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
            typer.secho("Name can't be empty.", fg=typer.colors.RED)
            continue
        if name in existing_names:
            typer.secho(f"A config named '{name}' already exists - choose another name.", fg=typer.colors.RED)
            continue
        return name


def run_init_wizard() -> None:
    typer.secho("gozu init", bold=True, underline=True)

    step_bootstrap_env()

    # Materializes docker-compose.yml/Dockerfile/sql/init.sql (+ the source
    # tree the worker/receiver images build from) into ~/.gozu/ - see
    # cli/stack/files.py. Postgres has to come up right here, before
    # `gozu up` is ever run, because create_config() below needs a live
    # connection to save this config at all.
    stack_dir = ensure_stack_files()
    typer.echo("Bringing up Postgres ...")
    ensure_postgres_up(stack_dir)

    step_ensure_java()

    typer.secho("Step 3/4: scanner + credentials", bold=True)
    scanner_type = _select_scanner()
    scanner_mode = select_scanner_mode()

    sonar_plan: str | None = None
    branches: str | None = None

    if scanner_mode == "local":
        credentials, trigger_mode, project_key = collect_local_sonar(stack_dir)
    else:
        credentials, trigger_mode, project_key, sonar_plan, branches = collect_cloud_sonar()

    ticket_destination_id = collect_jira()

    typer.secho("Step 4/4: name this config", bold=True)
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
