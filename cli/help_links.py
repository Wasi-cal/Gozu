# Copyright (c) 2026 Calfus Inc.
# Author: Wasiullah Rafeeq S

"""
Doc links shown before each credential prompt in the init wizard, so a user
who doesn't already know where e.g. a SonarQube token comes from isn't left
guessing. Every URL here was looked up live (Sonar/Atlassian docs), not
guessed - if a backend's docs move, update the entry here rather than
re-guessing a plausible-looking URL.

Keyed by a wizard-internal field name, not always the literal credential
key stored in config_credentials (sonar_token has two entries, since the
"how do I get one" instructions differ completely between self-hosted
SonarQube and SonarQube Cloud).
"""

import typer

HELP_LINKS: dict[str, str] = {
    "sonar_token_local": "https://docs.sonarsource.com/sonarqube-server/latest/user-guide/managing-tokens/",
    "sonar_token_cloud": "https://docs.sonarsource.com/sonarqube-cloud/managing-your-account/managing-tokens",
    "sonar_organization": "https://docs.sonarsource.com/sonarqube-cloud/getting-started/viewing-organizations",
    "sonar_project_key": "https://docs.sonarsource.com/sonarqube-server/project-administration/maintaining-project/changing-project-key",
    "sonar_branch": "https://docs.sonarsource.com/sonarqube-cloud/analyzing-source-code/branch-analysis/branch-analysis",
    "jira_api_token": "https://id.atlassian.com/manage-profile/security/api-tokens",
    "jira_email": "https://id.atlassian.com/manage-profile/profile-and-visibility",
    "jira_project_key": "https://confluence.atlassian.com/jirakb/how-to-get-project-id-from-the-jira-user-interface-827341414.html",
    "webhook_secret": "https://docs.sonarsource.com/sonarqube-cloud/managing-your-projects/administering-your-projects/integrations/webhooks",
}


def print_help_link(field: str) -> None:
    """Print a dimmed `-> docs: <url>` line for `field`, if a link is registered for it."""
    url = HELP_LINKS.get(field)
    if url:
        typer.secho(f"  -> docs: {url}", dim=True)
