"""
Jira ticket-backend credential collection for the init wizard - either
reuse an existing shared ticket_destinations row (config/ticket_destinations.py)
so multiple configs can point at the same board without duplicating
credentials, or create a new one.
"""

import questionary
import typer

import config.store as config_store
from cli.init_wizard.prompts import ask_or_exit, prompt_text

_CREATE_NEW = "__create_new__"


def _prompt_destination_name() -> str:
    existing_names = {d["name"] for d in config_store.list_ticket_destinations()}
    while True:
        name = ask_or_exit(questionary.text("Name this ticket destination:")).strip()
        if not name:
            typer.secho("Name can't be empty.", fg=typer.colors.RED)
            continue
        if name in existing_names:
            typer.secho(f"A ticket destination named '{name}' already exists - choose another name.", fg=typer.colors.RED)
            continue
        return name


def _create_new_destination() -> int:
    typer.secho("Jira details", bold=True)
    credentials = {
        "jira_url": prompt_text("Jira URL (e.g. https://your-domain.atlassian.net):"),
        "jira_email": prompt_text("Jira account email:", field="jira_email"),
        "jira_api_token": prompt_text("Jira API token:", field="jira_api_token"),
    }
    project_key = prompt_text("Jira project key:", field="jira_project_key")
    name = _prompt_destination_name()

    return config_store.create_ticket_destination(
        name=name, ticket_backend="jira", project_key=project_key, credentials=credentials
    )


def collect_jira() -> int:
    """
    Returns the id of the ticket_destinations row this config should use -
    either an existing one the user picked, or a freshly created one. New
    configs always go through this path now; only pre-existing configs
    still carry their Jira credentials embedded in their own
    config_credentials (see config/store.py's get_config()).
    """
    destinations = config_store.list_ticket_destinations()
    if not destinations:
        return _create_new_destination()

    choice = ask_or_exit(
        questionary.select(
            "Use an existing ticket destination, or create a new one?",
            choices=[questionary.Choice(title=d["name"], value=d["name"]) for d in destinations]
            + [questionary.Choice(title="Create a new ticket destination", value=_CREATE_NEW)],
        )
    )
    if choice == _CREATE_NEW:
        return _create_new_destination()

    destination = config_store.get_ticket_destination(choice)
    if destination is None:
        raise RuntimeError(f"Ticket destination '{choice}' disappeared before it could be loaded.")
    return destination["id"]
