# Copyright (c) 2026 Calfus Inc.
# Author: Wasiullah Rafeeq S
#
# Depends on: config/store.py - listing existing ticket destinations to offer for reuse

"""
Jira ticket-backend credential collection for the init wizard - either
reuse an existing shared ticket_destinations row (config/ticket_destinations.py)
so multiple configs can point at the same board without duplicating
credentials, or create a new one.

choose_jira_destination() is deliberately structural/one-shot (same
category as scanner_mode/sonar_plan) and does NOT create anything itself
- reusing an existing destination has nothing further to collect at all;
creating a new one only decides that a destination WILL be created, with
its fields (name, jira_url, jira_email, jira_api_token, jira_project_key)
collected via cli/wizard_engine.py like every other value-level field, so
they can be corrected on the review screen before anything is committed.
The actual config_store.create_ticket_destination() call happens once,
at final commit, in cli/init_wizard/__init__.py - not here.
"""

import questionary

import config.store as config_store
from cli.prompts import ask_or_exit, prompt_text
from cli.status import error

_CREATE_NEW = "__create_new__"


def prompt_jira_url(default: str = "") -> str:
    return prompt_text("Jira URL (e.g. https://your-domain.atlassian.net):", default=default)


def prompt_jira_email(default: str = "") -> str:
    return prompt_text("Jira account email:", field="jira_email", default=default)


def prompt_jira_api_token(default: str = "") -> str:
    return prompt_text("Jira API token:", field="jira_api_token", default=default)


def prompt_jira_project_key(default: str = "") -> str:
    return prompt_text("Jira project key:", field="jira_project_key", default=default)


def prompt_destination_name() -> str:
    existing_names = {d["name"] for d in config_store.list_ticket_destinations()}
    while True:
        name = ask_or_exit(questionary.text("Name this ticket destination:")).strip()
        if not name:
            error("Name can't be empty.")
            continue
        if name in existing_names:
            error(f"A ticket destination named '{name}' already exists - choose another name.")
            continue
        return name


def choose_jira_destination() -> tuple[str, int | None]:
    """
    Returns ("existing", destination_id) if the user picked one already
    in the store - fully set up already, nothing more to collect for
    Jira at all this run. Returns ("new", None) if creating one - the
    caller is responsible for adding the new destination's fields to the
    wizard's field list and committing them via create_ticket_destination()
    itself once the review screen is done, not here.
    """
    destinations = config_store.list_ticket_destinations()
    if not destinations:
        return "new", None

    choice = ask_or_exit(
        questionary.select(
            "Use an existing ticket destination, or create a new one?",
            choices=[questionary.Choice(title=d["name"], value=d["name"]) for d in destinations]
            + [questionary.Choice(title="Create a new ticket destination", value=_CREATE_NEW)],
        )
    )
    if choice == _CREATE_NEW:
        return "new", None

    destination = config_store.get_ticket_destination(choice)
    if destination is None:
        raise RuntimeError(f"Ticket destination '{choice}' disappeared before it could be loaded.")
    return "existing", destination["id"]
