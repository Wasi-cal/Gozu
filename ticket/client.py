"""
Generic ticketing interface plus the Jira implementation.

Adding a new ticket destination (Linear, GitHub Issues, whatever) means
writing a new TicketClient subclass here and registering it in
get_ticket_client() - nothing in core/models.py, scanner/client.py, or the
Temporal workflow/activities/receiver needs to change.
"""

import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

import requests
from temporalio import activity

from core.models import Finding, Severity

# Normalized Severity -> Jira priority name.
SEVERITY_TO_PRIORITY = {
    Severity.CRITICAL: "Highest",
    Severity.HIGH: "High",
    Severity.MEDIUM: "Medium",
    Severity.LOW: "Low",
    Severity.INFO: "Low",
}
DEFAULT_PRIORITY = "Medium"

SUMMARY_MAX_LENGTH = 255


class TicketClient(ABC):
    @abstractmethod
    def find_existing(self, finding_key: str) -> str | None:
        """Return the key of an existing ticket for this finding, if any (dedupe)."""
        raise NotImplementedError

    @abstractmethod
    def create_ticket(self, finding: Finding) -> str:
        """Create a ticket for a finding, return the new ticket's key."""
        raise NotImplementedError


class JiraClient(TicketClient):
    def __init__(self, base_url: str, email: str, api_token: str, project_key: str):
        self.base_url = base_url.rstrip("/")
        self.project_key = project_key
        self.auth = (email, api_token)
        self.headers = {"Content-Type": "application/json"}
        self._active_sprint_id = None
        self._active_sprint_looked_up = False

    def _raise_for_status(self, response: requests.Response, action: str):
        if not (200 <= response.status_code < 300):
            raise RuntimeError(
                f"Jira {action} failed with status {response.status_code}: {response.text}"
            )

    def _get_active_sprint_id(self) -> int | None:
        """
        Find the active sprint for this project's (first) Agile board, if
        any. Cached per-client instance since it doesn't change mid-run.
        """
        if self._active_sprint_looked_up:
            return self._active_sprint_id
        self._active_sprint_looked_up = True

        boards_response = requests.get(
            f"{self.base_url}/rest/agile/1.0/board",
            params={"projectKeyOrId": self.project_key},
            auth=self.auth,
        )
        self._raise_for_status(boards_response, "board lookup")
        boards = boards_response.json().get("values", [])
        if not boards:
            activity.logger.warning(
                f"No Agile board found for project {self.project_key}; new tickets will stay in the backlog"
            )
            return None

        board_id = boards[0]["id"]
        sprints_response = requests.get(
            f"{self.base_url}/rest/agile/1.0/board/{board_id}/sprint",
            params={"state": "active"},
            auth=self.auth,
        )
        self._raise_for_status(sprints_response, "sprint lookup")
        sprints = sprints_response.json().get("values", [])
        if not sprints:
            activity.logger.warning(
                f"No active sprint on board {board_id}; new tickets will stay in the backlog"
            )
            return None

        self._active_sprint_id = sprints[0]["id"]
        return self._active_sprint_id

    def _add_issue_to_active_sprint(self, issue_key: str):
        sprint_id = self._get_active_sprint_id()
        if sprint_id is None:
            return

        response = requests.post(
            f"{self.base_url}/rest/agile/1.0/sprint/{sprint_id}/issue",
            json={"issues": [issue_key]},
            auth=self.auth,
            headers=self.headers,
        )
        self._raise_for_status(response, "add issue to sprint")

    def find_existing(self, finding_key: str) -> str | None:
        """
        Search for a Jira ticket already tagged with the source-key-{key}
        label. Returns the issue key (e.g. "PROJ-123") if found, else None.
        """
        label = f"source-key-{finding_key}"
        jql = f'project = {self.project_key} AND labels = "{label}"'

        params: dict[str, Any] = {"jql": jql, "fields": "key", "maxResults": 1}
        response = requests.get(
            f"{self.base_url}/rest/api/3/search/jql",
            params=params,
            auth=self.auth,
            headers=self.headers,
        )
        self._raise_for_status(response, "search")

        data = response.json()
        issues = data.get("issues", [])
        if issues:
            return issues[0]["key"]
        return None

    def _map_priority(self, severity: Severity) -> str:
        return SEVERITY_TO_PRIORITY.get(severity, DEFAULT_PRIORITY)

    def _build_summary(self, finding: Finding) -> str:
        summary = finding.title
        if len(summary) > SUMMARY_MAX_LENGTH:
            summary = summary[: SUMMARY_MAX_LENGTH - 3] + "..."
        return summary

    def _build_description_adf(self, finding: Finding) -> dict:
        return {
            "type": "doc",
            "version": 1,
            "content": [
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": finding.message}],
                },
                {
                    "type": "bulletList",
                    "content": [
                        {
                            "type": "listItem",
                            "content": [
                                {
                                    "type": "paragraph",
                                    "content": [{"type": "text", "text": f"Component: {finding.component}"}],
                                }
                            ],
                        },
                        {
                            "type": "listItem",
                            "content": [
                                {"type": "paragraph", "content": [{"type": "text", "text": f"Line: {finding.line}"}]}
                            ],
                        },
                        {
                            "type": "listItem",
                            "content": [
                                {
                                    "type": "paragraph",
                                    "content": [{"type": "text", "text": f"Type: {finding.finding_type}"}],
                                }
                            ],
                        },
                        {
                            "type": "listItem",
                            "content": [
                                {
                                    "type": "paragraph",
                                    "content": [{"type": "text", "text": f"Severity: {finding.severity.value}"}],
                                }
                            ],
                        },
                        {
                            "type": "listItem",
                            "content": [
                                {
                                    "type": "paragraph",
                                    "content": [{"type": "text", "text": f"Source: {finding.source_tool}"}],
                                }
                            ],
                        },
                        {
                            "type": "listItem",
                            "content": [
                                {
                                    "type": "paragraph",
                                    "content": [{"type": "text", "text": f"Branch: {finding.branch or 'unknown'}"}],
                                }
                            ],
                        },
                        {
                            "type": "listItem",
                            "content": [
                                {
                                    "type": "paragraph",
                                    "content": [
                                        {
                                            "type": "text",
                                            "text": finding.deep_link,
                                            "marks": [{"type": "link", "attrs": {"href": finding.deep_link}}],
                                        }
                                    ],
                                }
                            ],
                        },
                    ],
                },
            ],
        }

    def create_ticket(self, finding: Finding) -> str:
        """Create a Jira issue for a finding, return the new issue key."""
        payload: dict[str, Any] = {
            "fields": {
                "project": {"key": self.project_key},
                "summary": self._build_summary(finding),
                "issuetype": {"name": "Bug"},
                "priority": {"name": self._map_priority(finding.severity)},
                "labels": ["sonarqube", "security", f"source-key-{finding.key}"],
                "description": self._build_description_adf(finding),
            }
        }

        response = requests.post(
            f"{self.base_url}/rest/api/3/issue",
            json=payload,
            auth=self.auth,
            headers=self.headers,
        )
        self._raise_for_status(response, "create issue")
        issue_key = response.json()["key"]

        self._add_issue_to_active_sprint(issue_key)

        return issue_key

    def attach_screenshot(self, issue_key: str, image_path: Path) -> None:
        """
        Attach a screenshot file to an existing issue. Skips the upload if a
        file with the same name is already attached, so retries of the
        calling activity don't create duplicate attachments.

        Not part of the generic TicketClient contract - it's a Jira-specific
        bonus capability, called directly by the screenshot activity rather
        than through get_ticket_client()'s abstract interface.
        """
        get_response = requests.get(
            f"{self.base_url}/rest/api/3/issue/{issue_key}",
            params={"fields": "attachment"},
            auth=self.auth,
            headers=self.headers,
        )
        self._raise_for_status(get_response, "get attachments")

        existing = get_response.json().get("fields", {}).get("attachment", [])
        if any(attachment.get("filename") == image_path.name for attachment in existing):
            activity.logger.warning(
                f"Attachment {image_path.name} already exists on {issue_key}; skipping upload"
            )
            return

        with open(image_path, "rb") as f:
            response = requests.post(
                f"{self.base_url}/rest/api/3/issue/{issue_key}/attachments",
                auth=self.auth,
                headers={"X-Atlassian-Token": "no-check"},
                files={"file": (image_path.name, f, "image/png")},
            )
        self._raise_for_status(response, "attach screenshot")

    def add_comment(self, ticket_key: str, body: str) -> None:
        """
        Add a comment to an existing ticket, rendering `body` as a single
        Jira code-block node.

        Not part of the generic TicketClient contract - it's a Jira-specific
        bonus capability, called directly by the screenshot activity rather
        than through get_ticket_client()'s abstract interface.
        """
        payload: dict[str, Any] = {
            "body": {
                "type": "doc",
                "version": 1,
                "content": [
                    {"type": "codeBlock", "attrs": {}, "content": [{"type": "text", "text": body}]}
                ],
            }
        }
        response = requests.post(
            f"{self.base_url}/rest/api/3/issue/{ticket_key}/comment",
            json=payload,
            auth=self.auth,
            headers=self.headers,
        )
        self._raise_for_status(response, "add comment")


def build_ticket_client(ticket_backend: str, credentials: dict[str, str]) -> TicketClient:
    """
    Pure constructor: build a TicketClient from explicit params, no env
    reads. This is what a specific config's credentials (config_store.
    get_config(), as used by `codescan run`) go through; get_ticket_client()
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
