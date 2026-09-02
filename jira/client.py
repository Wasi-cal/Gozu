"""
Minimal Jira Cloud REST API client used to create tickets for SonarQube
issues, check whether a ticket already exists for a given issue (dedupe),
and place newly-created tickets into the project's active sprint.
"""

import logging
from pathlib import Path

import requests

from sonar.models import SonarIssue

logger = logging.getLogger(__name__)

SEVERITY_TO_PRIORITY = {
    "BLOCKER": "Highest",
    "CRITICAL": "Highest",
    "MAJOR": "High",
    "MINOR": "Medium",
    "INFO": "Low",
}
DEFAULT_PRIORITY = "Medium"

SUMMARY_MAX_LENGTH = 255


class JiraClient:
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
            logger.warning(f"No Agile board found for project {self.project_key}; new tickets will stay in the backlog")
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
            logger.warning(f"No active sprint on board {board_id}; new tickets will stay in the backlog")
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

    def find_existing_ticket(self, sonar_issue_key: str) -> str | None:
        """
        Search for a Jira ticket already tagged with the sonar-key-{key}
        label. Returns the issue key (e.g. "PROJ-123") if found, else None.
        """
        label = f"sonar-key-{sonar_issue_key}"
        jql = f'project = {self.project_key} AND labels = "{label}"'

        response = requests.get(
            f"{self.base_url}/rest/api/3/search/jql",
            params={"jql": jql, "fields": "key", "maxResults": 1},
            auth=self.auth,
            headers=self.headers,
        )
        self._raise_for_status(response, "search")

        data = response.json()
        issues = data.get("issues", [])
        if issues:
            return issues[0]["key"]
        return None

    def _map_priority(self, severity: str | None) -> str:
        if not severity or severity not in SEVERITY_TO_PRIORITY:
            logger.warning(
                f"Unrecognized or missing severity '{severity}', defaulting priority to '{DEFAULT_PRIORITY}'"
            )
            return DEFAULT_PRIORITY
        return SEVERITY_TO_PRIORITY[severity]

    def _build_description_adf(self, issue: SonarIssue) -> dict:
        return {
            "type": "doc",
            "version": 1,
            "content": [
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": issue.message}],
                },
                {
                    "type": "bulletList",
                    "content": [
                        {
                            "type": "listItem",
                            "content": [
                                {"type": "paragraph", "content": [{"type": "text", "text": f"Rule: {issue.rule}"}]}
                            ],
                        },
                        {
                            "type": "listItem",
                            "content": [
                                {
                                    "type": "paragraph",
                                    "content": [{"type": "text", "text": f"Component: {issue.component}"}],
                                }
                            ],
                        },
                        {
                            "type": "listItem",
                            "content": [
                                {"type": "paragraph", "content": [{"type": "text", "text": f"Line: {issue.line}"}]}
                            ],
                        },
                        {
                            "type": "listItem",
                            "content": [
                                {"type": "paragraph", "content": [{"type": "text", "text": f"Type: {issue.type}"}]}
                            ],
                        },
                        {
                            "type": "listItem",
                            "content": [
                                {
                                    "type": "paragraph",
                                    "content": [{"type": "text", "text": f"Branch: {issue.branch or 'unknown'}"}],
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
                                            "text": issue.deep_link,
                                            "marks": [{"type": "link", "attrs": {"href": issue.deep_link}}],
                                        }
                                    ],
                                }
                            ],
                        },
                    ],
                },
            ],
        }

    def create_ticket(self, issue: SonarIssue) -> str:
        """Create a Jira issue for a SonarQube issue, return the new issue key."""
        summary = f"[Sonar] {issue.rule} in {issue.component}:{issue.line}"
        if len(summary) > SUMMARY_MAX_LENGTH:
            summary = summary[: SUMMARY_MAX_LENGTH - 3] + "..."

        payload = {
            "fields": {
                "project": {"key": self.project_key},
                "summary": summary,
                "issuetype": {"name": "Bug"},
                "priority": {"name": self._map_priority(issue.severity)},
                "labels": ["sonarqube", "security", f"sonar-key-{issue.key}"],
                "description": self._build_description_adf(issue),
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
            logger.warning(f"Attachment {image_path.name} already exists on {issue_key}; skipping upload")
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
        """Add a comment to an existing ticket, rendering `body` as a single
        Jira code-block node."""
        payload = {
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
