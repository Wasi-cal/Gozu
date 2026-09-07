# Copyright (c) 2026 Calfus Inc.
# Author: Wasiullah Rafeeq S
# Editor: Prakrit Mohanty, Claude
#
# Depends on: ticket/jira_client.py - dispatches to JiraClient for ticket_backend "jira"

"""
Pure-builder + env-reading-wrapper pair for constructing a TicketClient -
see cli/prerequisites and scanner/factory.py for the same pattern.
"""

import os

from ticket.base import TicketClient
from ticket.jira_client import JiraClient


def build_ticket_client(ticket_backend: str, credentials: dict[str, str]) -> TicketClient:
    """
    Pure constructor: build a TicketClient from explicit params, no env
    reads. This is what a specific config's credentials (config.store.
    get_config(), as used by `gozu run`) go through; get_ticket_client()
    below is a thin env-reading wrapper around this for the legacy
    single-global-config path (the webhook receiver).
    """
    if ticket_backend != "jira":
        raise ValueError(f"Unrecognized ticket_backend '{ticket_backend}'. Expected 'jira'.")

    return JiraClient(
        base_url=credentials["jira_url"],
        email=credentials["jira_email"],
        api_token=credentials["jira_api_token"],
        project_key=credentials["jira_project_key"],
    )


def get_ticket_client() -> TicketClient:
    """
    Legacy path used by the webhook receiver, which has no per-config
    credentials of its own: reads TICKET_BACKEND/JIRA_* from the
    environment and delegates to build_ticket_client(). This is the only
    place in the codebase that should ever read those env vars for this
    purpose.
    """
    backend = os.environ.get("TICKET_BACKEND", "jira")
    credentials = {
        "jira_url": os.environ["JIRA_URL"],
        "jira_email": os.environ["JIRA_EMAIL"],
        "jira_api_token": os.environ["JIRA_API_TOKEN"],
        "jira_project_key": os.environ["JIRA_PROJECT_KEY"],
    }
    return build_ticket_client(backend, credentials)
