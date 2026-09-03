"""Jira ticket-backend credential collection for the init wizard."""

import typer

from cli.init_wizard.prompts import prompt_text


def collect_jira() -> dict[str, str]:
    typer.secho("Jira details", bold=True)
    return {
        "jira_url": prompt_text("Jira URL (e.g. https://your-domain.atlassian.net):"),
        "jira_email": prompt_text("Jira account email:", field="jira_email"),
        "jira_api_token": prompt_text("Jira API token:", field="jira_api_token"),
        "jira_project_key": prompt_text("Jira project key:", field="jira_project_key"),
    }
