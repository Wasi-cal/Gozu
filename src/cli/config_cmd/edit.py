# Copyright (c) 2026 Calfus Inc.
# Author: Wasiullah Rafeeq S
# Editor: Prakrit Mohanty
#
# Depends on: src/config/store.py - reading and updating a config's fields/credentials
# Depends on: src/cli/config_lookup.py - resolving a config name typed by the user
# Depends on: src/cli/init_wizard/jira_step.py - reusing its Jira credential prompt functions
# Depends on: src/cli/init_wizard/llm_step.py - reusing its Anthropic API key prompt function
# Depends on: src/cli/init_wizard/sonar_cloud.py - reusing its SonarQube Cloud credential prompt functions
# Depends on: src/cli/init_wizard/sonar_local.py - reusing its SonarQube Local credential prompt functions
# Depends on: src/cli/stack/__init__.py - ensuring the config store is ready before editing
# Depends on: src/cli/wizard_engine.py - the shared review/edit engine this command drives

"""
`gozu config edit <name>` - reuses src/cli/wizard_engine.py, skipping
straight to the review screen pre-populated from the config's current
values. scanner_type/scanner_mode/sonar_plan/trigger_mode are structural
(decided once at `gozu init` time) and are never offered here.
"""

import questionary
import typer

import config.store as config_store
from cli.config_lookup import resolve_config_or_prompt
from cli.init_wizard.jira_step import (
    prompt_jira_api_token,
    prompt_jira_email,
    prompt_jira_project_key,
    prompt_jira_url,
)
from cli.init_wizard.llm_step import prompt_anthropic_api_key
from cli.init_wizard.semgrep_step import prompt_project_label
from cli.init_wizard.sonar_cloud import (
    prompt_premium_branches,
    prompt_sonar_organization,
    prompt_sonar_token_cloud,
)
from cli.init_wizard.sonar_local import prompt_project_key, prompt_sonar_token_local
from cli.prompts import ask_or_exit, generate_or_prompt_secret, prompt_text
from cli.stack import ensure_config_store_ready
from cli.status import success, warning
from cli.wizard_engine import WizardField, run_wizard

# Mirrors src/config/store.py's own _DESTINATION_RESOLVED_KEYS - decides
# whether to show the multi-config warning before writing.
_DESTINATION_RESOLVED_KEYS = {"jira_url", "jira_email", "jira_api_token", "jira_project_key"}

# Plain columns (config_store.update_config_fields()); every other
# editable field is a credential (update_config_credential()).
_PLAIN_COLUMN_KEYS = {"project_key", "branches", "ticket_cap"}


def _prompt_ticket_cap(current: int | None) -> int | None:
    """The persistent per-config default (`gozu run -t` is a separate,
    one-off override). Blank always means "use the built-in fallback",
    never an explicit 0."""
    default = str(current) if current is not None else ""
    while True:
        answer = ask_or_exit(
            questionary.text("Per-run ticket cap (blank = use the default):", default=default)
        )
        answer = answer.strip()
        if not answer:
            return None
        try:
            value = int(answer)
        except ValueError:
            warning("Enter a positive whole number, or leave blank to use the default.")
            continue
        if value <= 0:
            warning("Enter a positive whole number, or leave blank to use the default.")
            continue
        return value


def _build_edit_fields(config: dict, state: dict) -> list[WizardField]:
    credentials = config["credentials"]
    scanner_type = config["scanner_type"]
    scanner_mode = config["scanner_mode"]
    trigger_mode = config["trigger_mode"]

    state["ticket_cap"] = config.get("ticket_cap")
    project_key_field = (
        None
        if scanner_type == "trivy"
        # No project_key, no credentials, no branches at all - see
        # src/cli/init_wizard/__init__.py's _build_fields() Trivy branch
        # for why. Only ticket_cap and the shared Jira fields below apply.
        else WizardField("project_key", "Project label", lambda: prompt_project_label(state.get("project_key", "")))
        if scanner_type == "semgrep"
        else WizardField(
            "project_key", "SonarQube project key", lambda: prompt_project_key(state.get("project_key", ""))
        )
    )
    fields = [
        *([project_key_field] if project_key_field is not None else []),
        WizardField(
            "ticket_cap",
            "Per-run ticket cap (blank = default)",
            lambda: _prompt_ticket_cap(state.get("ticket_cap")),
        ),
    ]

    if project_key_field is not None:
        state["project_key"] = config.get("project_key") or ""

    if scanner_type in ("trivy", "semgrep"):
        pass  # no scanner credentials to edit - trivy needs none, semgrep's trigger_mode "direct" needs none
    elif scanner_mode == "local":
        state["sonar_token"] = credentials.get("sonar_token", "")
        state["sonar_host_url"] = credentials.get("sonar_host_url", "")
        fields += [
            WizardField(
                "sonar_token", "SonarQube token", lambda: prompt_sonar_token_local(state.get("sonar_token", "")), secret=True
            ),
            WizardField(
                "sonar_host_url",
                "SonarQube host URL",
                lambda: prompt_text("SonarQube host URL:", default=state.get("sonar_host_url", "")),
            ),
        ]
    else:
        state["sonar_token"] = credentials.get("sonar_token", "")
        state["sonar_organization"] = credentials.get("sonar_organization", "")
        state["branches"] = config.get("branches") or ""
        fields += [
            WizardField(
                "sonar_token",
                "SonarQube Cloud token",
                lambda: prompt_sonar_token_cloud(state.get("sonar_token", "")),
                secret=True,
            ),
            WizardField(
                "sonar_organization",
                "SonarQube Cloud organization key",
                lambda: prompt_sonar_organization(state.get("sonar_organization", "")),
            ),
            # prompt_premium_branches()'s comma-join degrades to a single
            # value for a one-branch string, so it's reused here rather than
            # adding a third near-identical branches prompt just for edit.
            WizardField(
                "branches", "Branches to track", lambda: prompt_premium_branches(state.get("branches", "main"))
            ),
        ]

    if trigger_mode == "webhook":
        state["webhook_secret"] = credentials.get("webhook_secret", "")
        fields.append(
            WizardField("webhook_secret", "Webhook secret", lambda: generate_or_prompt_secret("Webhook secret"), secret=True)
        )

    state["jira_url"] = credentials.get("jira_url", "")
    state["jira_email"] = credentials.get("jira_email", "")
    state["jira_api_token"] = credentials.get("jira_api_token", "")
    state["jira_project_key"] = credentials.get("jira_project_key", "")
    fields += [
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

    state["anthropic_api_key"] = credentials.get("anthropic_api_key", "")
    fields.append(
        WizardField(
            "anthropic_api_key",
            "Anthropic API key (optional)",
            lambda: prompt_anthropic_api_key(state.get("anthropic_api_key", "")),
            secret=True,
        )
    )
    return fields


def edit_command(name: str) -> None:
    ensure_config_store_ready()
    config = resolve_config_or_prompt(name)
    # Use the resolved config's own name from here - resolve_config_or_prompt()
    # may have picked a different one via its "did you mean" recovery picker.
    name = config["name"]

    state: dict = {}
    fields = _build_edit_fields(config, state)
    original = dict(state)

    final_state = run_wizard(fields, state, walk_first=False)
    changed_keys = [key for key, value in final_state.items() if original.get(key) != value]

    if not changed_keys:
        typer.echo("Nothing changed.")
        return

    changed_destination_keys = [key for key in changed_keys if key in _DESTINATION_RESOLVED_KEYS]
    destination_id = config.get("ticket_destination_id")
    if changed_destination_keys and destination_id is not None:
        affected_count = config_store.count_configs_using_destination(destination_id, exclude_config_name=name)
        if affected_count > 0:
            warning(
                f"'{name}' shares its Jira credentials with {affected_count} other config(s) via a shared ticket "
                f"destination - changing {', '.join(changed_destination_keys)} will affect them too, not just '{name}'."
            )
            if not ask_or_exit(questionary.confirm("Continue anyway?", default=False)):
                typer.echo("Cancelled - nothing was changed.")
                return

    plain_field_updates = {key: final_state[key] for key in changed_keys if key in _PLAIN_COLUMN_KEYS}
    if plain_field_updates:
        config_store.update_config_fields(name, **plain_field_updates)

    for key in changed_keys:
        if key in _PLAIN_COLUMN_KEYS:
            continue
        config_store.update_config_credential(name, key, final_state[key])

    success(f"Updated '{name}': {', '.join(changed_keys)}")
