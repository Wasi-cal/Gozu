"""Jira implementation of TicketClient (see ticket/base.py for the contract)."""

from pathlib import Path
from typing import Any

import requests
from temporalio import activity

from core.models import Finding, Severity
from ticket.adf import bullet_list, code_block, doc, paragraph
from ticket.base import (
    DEFAULT_PRIORITY,
    SEVERITY_TO_PRIORITY,
    SUMMARY_MAX_LENGTH,
    TicketClient,
)
from ticket.jira_sprint import SprintAssigner


class JiraClient(TicketClient):
    def __init__(self, base_url: str, email: str, api_token: str, project_key: str):
        self.base_url = base_url.rstrip("/")
        self.project_key = project_key
        self.auth = (email, api_token)
        self.headers = {"Content-Type": "application/json"}
        self._sprints = SprintAssigner(self.base_url, self.auth, self.headers, project_key)

    def destination_id(self) -> str:
        return f"jira:{self.base_url}:{self.project_key}"

    def _raise_for_status(self, response: requests.Response, action: str):
        if not (200 <= response.status_code < 300):
            raise RuntimeError(
                f"Jira {action} failed with status {response.status_code}: {response.text}"
            )

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

        issues = response.json().get("issues", [])
        return issues[0]["key"] if issues else None

    def _map_priority(self, severity: Severity) -> str:
        return SEVERITY_TO_PRIORITY.get(severity, DEFAULT_PRIORITY)

    def _build_summary(self, finding: Finding) -> str:
        summary = finding.title
        if len(summary) > SUMMARY_MAX_LENGTH:
            summary = summary[: SUMMARY_MAX_LENGTH - 3] + "..."
        return summary

    def _build_description(self, finding: Finding) -> dict:
        details = [
            f"Component: {finding.component}",
            f"Line: {finding.line}",
            f"Type: {finding.finding_type}",
            f"Severity: {finding.severity.value}",
            f"Source: {finding.source_tool}",
            f"Branch: {finding.branch or 'unknown'}",
        ]
        return doc(
            paragraph(finding.message),
            bullet_list(details),
            {"type": "bulletList", "content": [{"type": "listItem", "content": [paragraph(finding.deep_link, link=finding.deep_link)]}]},
        )

    def create_ticket(self, finding: Finding) -> str:
        """Create a Jira issue for a finding, return the new issue key."""
        payload: dict[str, Any] = {
            "fields": {
                "project": {"key": self.project_key},
                "summary": self._build_summary(finding),
                "issuetype": {"name": "Bug"},
                "priority": {"name": self._map_priority(finding.severity)},
                "labels": ["sonarqube", "security", f"source-key-{finding.key}"],
                "description": self._build_description(finding),
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

        self._sprints.add_issue(issue_key)
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
        """Add a comment to an existing ticket, rendering `body` as a single Jira code-block node."""
        payload: dict[str, Any] = {"body": doc(code_block(body))}
        response = requests.post(
            f"{self.base_url}/rest/api/3/issue/{ticket_key}/comment",
            json=payload,
            auth=self.auth,
            headers=self.headers,
        )
        self._raise_for_status(response, "add comment")
