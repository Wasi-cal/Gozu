# Copyright (c) 2026 Calfus Inc.
# Author: Wasiullah Rafeeq S
# Editor: Prakrit Mohanty, Claude
#
# Depends on: config/store.py - creating/reading configs and ticket destinations
# Depends on: config/migrations.py - applying schema migrations before first use
# Depends on: cli/stack/cleanup.py - scoping interrupt cleanup to services this run starts
# Depends on: cli/stack/files.py - materializing the Docker stack before use
# Depends on: cli/stack/profiles.py - bringing Postgres up before saving a config
# Depends on: cli/wizard_engine.py - the shared review/edit engine this wizard drives
# Depends on: scanner/base.py - listing the registered scanner types to choose from

"""
`gozu init` - the interactive setup wizard. Provisions .env, checks/
installs prerequisites, walks through scanner + ticket credential
selection (via cli/wizard_engine.py's shared review/edit engine - same
one `gozu config edit` uses), and seeds one row via config.store.
create_config(). Ends at "a named config exists in Postgres" - actually
running a scan is cli/scan_runner.py.

Structural/branching questions (which scanner, Local vs Cloud, Free vs
Premium, reuse-vs-create a ticket destination) are resolved directly,
one-shot, exactly as before - they decide WHICH value-level fields exist
at all, and are never themselves revisable through the review screen
(see cli/wizard_engine.py's own docstring for why). Only the resulting
value fields (project_key, branches, and every credential) go through the
engine, so a fresh config's WizardField list looks different depending on
scanner_mode/sonar_plan/trigger_mode, built here rather than executed
inline one prompt after another.
"""

from pathlib import Path

import questionary
import typer

import config.store as config_store
from cli.init_wizard.env_step import step_bootstrap_env, step_ensure_prerequisites
from cli.init_wizard.jira_step import (
    choose_jira_destination,
    prompt_destination_name,
    prompt_jira_api_token,
    prompt_jira_email,
    prompt_jira_project_key,
    prompt_jira_url,
)
from cli.init_wizard.sonar_cloud import (
    confirm_free_plan_limitation,
    prompt_free_branch,
    prompt_premium_branches,
    prompt_sonar_organization,
    prompt_sonar_token_cloud,
    select_cloud_plan,
    show_premium_intro,
)
from cli.init_wizard.sonar_local import (
    ensure_local_sonarqube_host,
    prompt_project_key,
    prompt_sonar_token_local,
    select_scanner_mode,
)
from cli.init_wizard.summary import print_summary
from cli.prompts import ask_or_exit, generate_or_prompt_secret
from cli.stack.cleanup import InterruptCleanup
from cli.stack.files import ensure_stack_files
from cli.stack.profiles import ensure_postgres_up, is_service_up
from cli.status import error, success, waiting
from cli.wizard_engine import WizardField, run_wizard
from config.migrations import run_migrations
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


def _build_fields(
    state: dict, stack_dir: Path, cleanup: InterruptCleanup, scanner_mode: str
) -> tuple[list[WizardField], str, str | None]:
    """
    Resolves the remaining structural questions (SonarQube host/infra for
    Local; Free-vs-Premium for Cloud; reuse-vs-create a ticket
    destination) and returns the value-level WizardField list they imply,
    plus the trigger_mode/sonar_plan those structural answers fix
    directly (never part of the field list - see this module's own
    docstring). `state` is mutated with each structural, non-revisable
    value too (sonar_host_url; destination_choice/existing_destination_id
    aren't config fields at all, so they're returned rather than stashed
    in `state`) - every WizardField closure below reads/writes `state` by
    reference, per cli/wizard_engine.py's contract.
    """
    fields: list[WizardField] = []
    sonar_plan: str | None = None

    if scanner_mode == "local":
        state["sonar_host_url"] = ensure_local_sonarqube_host(stack_dir, cleanup)
        trigger_mode = "webhook"
        fields += [
            WizardField(
                "sonar_token",
                "SonarQube token",
                lambda: prompt_sonar_token_local(state.get("sonar_token", "")),
                secret=True,
            ),
            WizardField(
                "project_key", "SonarQube project key", lambda: prompt_project_key(state.get("project_key", ""))
            ),
            WizardField(
                "webhook_secret", "Webhook secret", lambda: generate_or_prompt_secret("Webhook secret"), secret=True
            ),
        ]
    else:
        plan = select_cloud_plan()
        fields += [
            WizardField(
                "sonar_organization",
                "SonarQube Cloud organization key",
                lambda: prompt_sonar_organization(state.get("sonar_organization", "")),
            ),
            WizardField(
                "sonar_token",
                "SonarQube Cloud token",
                lambda: prompt_sonar_token_cloud(state.get("sonar_token", "")),
                secret=True,
            ),
            WizardField(
                "project_key", "SonarQube project key", lambda: prompt_project_key(state.get("project_key", ""))
            ),
        ]
        if plan == "Free":
            confirm_free_plan_limitation()
            sonar_plan, trigger_mode = "free", "watch"
            fields.append(
                WizardField(
                    "branches", "Target branch to track", lambda: prompt_free_branch(state.get("branches", "main"))
                )
            )
            typer.echo("trigger_mode is set to 'watch' automatically - no other mechanism applies on the Free plan.")
        else:
            show_premium_intro()
            sonar_plan, trigger_mode = "premium", "webhook"
            fields.append(
                WizardField(
                    "branches",
                    "Branches to track",
                    lambda: prompt_premium_branches(state.get("branches", "main")),
                )
            )
            fields.append(
                WizardField(
                    "webhook_secret",
                    "Webhook secret",
                    lambda: generate_or_prompt_secret("Webhook secret"),
                    secret=True,
                )
            )
            typer.echo("trigger_mode is set to 'webhook' automatically - it's the only mechanism Premium needs.")

    destination_choice, existing_destination_id = choose_jira_destination()
    state["_destination_choice"] = destination_choice
    state["_existing_destination_id"] = existing_destination_id
    if destination_choice == "new":
        typer.secho("Jira details", bold=True)
        fields += [
            WizardField("destination_name", "Ticket destination name", prompt_destination_name),
            WizardField("jira_url", "Jira URL", lambda: prompt_jira_url(state.get("jira_url", ""))),
            WizardField("jira_email", "Jira account email", lambda: prompt_jira_email(state.get("jira_email", ""))),
            WizardField(
                "jira_api_token",
                "Jira API token",
                lambda: prompt_jira_api_token(state.get("jira_api_token", "")),
                secret=True,
            ),
            WizardField(
                "jira_project_key", "Jira project key", lambda: prompt_jira_project_key(state.get("jira_project_key", ""))
            ),
        ]

    return fields, trigger_mode, sonar_plan


def _commit(state: dict, name: str, scanner_type: str, scanner_mode: str, trigger_mode: str, sonar_plan: str | None) -> None:
    """
    Everything up to here was in-memory only - this is the one place
    `gozu init` actually writes to Postgres, called once, after "Looks
    good - save" is chosen. A new ticket destination (if that's what was
    chosen) is created first, since create_config() needs its id.
    """
    if state["_destination_choice"] == "new":
        ticket_destination_id = config_store.create_ticket_destination(
            name=state["destination_name"],
            ticket_backend="jira",
            project_key=state["jira_project_key"],
            credentials={
                "jira_url": state["jira_url"],
                "jira_email": state["jira_email"],
                "jira_api_token": state["jira_api_token"],
            },
        )
    else:
        ticket_destination_id = state["_existing_destination_id"]

    credentials: dict[str, str] = {"sonar_token": state["sonar_token"]}
    if scanner_mode == "local":
        credentials["sonar_host_url"] = state["sonar_host_url"]
    else:
        credentials["sonar_organization"] = state["sonar_organization"]
    if trigger_mode == "webhook":
        credentials["webhook_secret"] = state["webhook_secret"]

    config_store.create_config(
        name=name,
        scanner_type=scanner_type,
        scanner_mode=scanner_mode,
        ticket_backend="jira",
        trigger_mode=trigger_mode,
        project_key=state["project_key"],
        credentials=credentials,
        sonar_plan=sonar_plan,
        branches=state.get("branches"),
        ticket_destination_id=ticket_destination_id,
    )

    destination = config_store.get_ticket_destination_by_id(ticket_destination_id)
    destination_name = destination["name"] if destination else None
    print_summary(
        name, scanner_type, scanner_mode, state["project_key"], sonar_plan, state.get("branches"), trigger_mode, destination_name
    )


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

    # A genuinely fresh Postgres volume has no gozu schema at all yet -
    # migration 0001 is what creates it (see config/migrations.py), same
    # as `gozu up`'s own post-ensure_postgres_up() step. Without this,
    # _prompt_config_name()'s list_configs() call below (and _commit()'s
    # create_config() after it) would hit a bare "relation does not
    # exist" instead of gozu's very first run ever working at all.
    waiting("Applying database migrations ...")
    applied = run_migrations(stack_dir)
    if applied:
        success(f"Applied {len(applied)} migration(s): {', '.join(applied)}")

    typer.secho("Step 2/3: scanner + credentials", bold=True)
    scanner_type = _select_scanner()
    scanner_mode = select_scanner_mode()

    # Both scanner_modes run sonar-scanner on THIS host (see
    # step_ensure_prerequisites()'s docstring) - unconditional, not gated
    # on Local vs Cloud.
    step_ensure_prerequisites()

    state: dict = {}
    fields, trigger_mode, sonar_plan = _build_fields(state, stack_dir, cleanup, scanner_mode)
    state = run_wizard(fields, state, walk_first=True)

    typer.secho("Step 3/3: name this config", bold=True)
    name = _prompt_config_name()

    _commit(state, name, scanner_type, scanner_mode, trigger_mode, sonar_plan)


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

    # Materializes docker-compose.yml/Dockerfile/sql/ (+ the source
    # tree the worker/receiver images build from) into ~/.gozu/ - see
    # cli/stack/files.py. Pure file writes, no Docker interaction - safe to
    # do before the interrupt-cleanup scope even starts.
    stack_dir = ensure_stack_files()

    with InterruptCleanup(stack_dir) as cleanup:
        _run_init_wizard_body(stack_dir, cleanup)
