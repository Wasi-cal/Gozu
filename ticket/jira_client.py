# Copyright (c) 2026 Calfus Inc.
# Author: Wasiullah Rafeeq S
#
# Depends on: Jira (Atlassian) - direct API client

"""Jira implementation of TicketClient (see ticket/base.py for the contract)."""

from pathlib import Path
from typing import Any

import requests
from temporalio import activity

from core.errors import TicketAuthError, TicketValidationError
from core.models import Finding, Severity
from ticket.adf import bullet_list, code_block, doc, paragraph
from ticket.base import (
    DEFAULT_PRIORITY,
    SEVERITY_TO_PRIORITY,
    SUMMARY_MAX_LENGTH,
    TicketClient,
)
from ticket.jira_sprint import SprintAssigner

# Distinct from the per-finding "source-key-{key}" labels - identifies the
# one shared backlog-rollup ticket a project can have (see
# upsert_rollup_ticket()), so it can be found again on a later run instead
# of creating a second one.
ROLLUP_LABEL = "gozu-backlog-rollup"

# How many of the deferred findings' keys/rules/severities to actually
# list in the rollup ticket's description before summarizing the rest -
# a genuinely large backlog (hundreds of findings) would make for an
# unreadable ticket body otherwise.
_ROLLUP_DESCRIPTION_MAX_LINES = 50


class JiraClient(TicketClient):
    def __init__(self, base_url: str, email: str, api_token: str, project_key: str):
        self.base_url = base_url.rstrip("/")
        self.project_key = project_key
        self.auth = (email, api_token)
        self.headers = {"Content-Type": "application/json"}
        self._sprints = SprintAssigner(self.base_url, self.auth, self.headers, project_key)

    def destination_id(self) -> str:
        return f"jira:{self.base_url}:{self.project_key}"

    def _raise_for_status(self, response: requests.Response, action: str) -> None:
        """
        Shared by every Jira call this client makes - one place to
        distinguish permanent failures (never worth retrying) from
        everything else (404s, 429s, 5xxs, left as a generic RuntimeError,
        same retryable path as before this existed).
        """
        if 200 <= response.status_code < 300:
            return
        if response.status_code in (401, 403):
            raise TicketAuthError(f"Jira {action} failed with status {response.status_code} (invalid/expired token?): {response.text}")
        if response.status_code == 400:
            raise TicketValidationError(f"Jira {action} failed with status {response.status_code}: {response.text}")
        raise RuntimeError(f"Jira {action} failed with status {response.status_code}: {response.text}")

    def _find_by_label(self, label: str) -> str | None:
        """Search for a Jira ticket tagged with `label` in this project. Returns the issue key (e.g. "PROJ-123") if found, else None."""
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

    def find_existing(self, finding_key: str) -> str | None:
        """Already-ticketed finding? (dedupe) - source-key-{key} is stamped on every per-finding ticket at creation."""
        return self._find_by_label(f"source-key-{finding_key}")

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

        # The issue above is already created in Jira at this point - sprint
        # assignment is a bonus, best-effort step (see ticket/jira_sprint.py's
        # own fallbacks for "no board"/"no active sprint"/Kanban), and must
        # never be able to fail ticket creation itself. Any other failure
        # here (a network blip, an unexpected Jira response) gets the same
        # treatment: log and move on, not raise.
        try:
            self._sprints.add_issue(issue_key)
        except Exception as e:
            activity.logger.warning(f"Sprint assignment failed for {issue_key}, leaving it in the backlog: {e}")

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

    def get_transitions(self, issue_key: str) -> list[dict[str, Any]]:
        """Every transition currently available on `issue_key`, in whatever workflow this project actually uses."""
        response = requests.get(
            f"{self.base_url}/rest/api/3/issue/{issue_key}/transitions",
            auth=self.auth,
            headers=self.headers,
        )
        self._raise_for_status(response, "get transitions")
        return response.json().get("transitions", [])

    def transition_to_done(self, issue_key: str) -> bool:
        """
        Move `issue_key` to whichever available transition leads to a
        "done"-category status - workflows vary per project, so this
        deliberately never hardcodes a status name like "Done"/"Closed",
        only the statusCategory.key Jira itself guarantees. Returns False
        (and does nothing) if no such transition is currently available,
        rather than guessing at the wrong one.
        """
        for transition in self.get_transitions(issue_key):
            if transition.get("to", {}).get("statusCategory", {}).get("key") != "done":
                continue
            response = requests.post(
                f"{self.base_url}/rest/api/3/issue/{issue_key}/transitions",
                json={"transition": {"id": transition["id"]}},
                auth=self.auth,
                headers=self.headers,
            )
            self._raise_for_status(response, "transition issue")
            return True
        return False

    def _update_ticket(self, issue_key: str, summary: str, description: dict) -> None:
        payload = {"fields": {"summary": summary, "description": description}}
        response = requests.put(
            f"{self.base_url}/rest/api/3/issue/{issue_key}",
            json=payload,
            auth=self.auth,
            headers=self.headers,
        )
        self._raise_for_status(response, "update issue")

    def _build_rollup_summary(self, count: int) -> str:
        return f"SonarQube backlog: {count} additional finding(s) not yet ticketed"

    def _build_rollup_description(self, remaining: list[Finding]) -> dict:
        lines = [
            f"{finding.key} - {finding.finding_type} - {finding.severity.value} - {finding.title}"
            for finding in remaining[:_ROLLUP_DESCRIPTION_MAX_LINES]
        ]
        if len(remaining) > _ROLLUP_DESCRIPTION_MAX_LINES:
            lines.append(f"... and {len(remaining) - _ROLLUP_DESCRIPTION_MAX_LINES} more")

        return doc(
            paragraph(
                "These findings were detected but not individually ticketed this run "
                "(per-run backlog cap reached) - they'll get their own ticket automatically "
                "in a future run as capacity frees up."
            ),
            bullet_list(lines),
        )

    def upsert_rollup_ticket(self, remaining: list[Finding]) -> str | None:
        """
        One shared ticket for however many findings didn't get their own
        this run (see create_tickets_activity's BACKLOG_CAP) - never one
        ticket per remaining finding. Checked by ROLLUP_LABEL every call:
        an existing rollup ticket gets its summary/description updated in
        place (count included) rather than a new one created alongside it
        - --watch/webhook mode re-runs this every cycle against what's
        likely the same persistent backlog, so this must never spam a new
        "N more findings" ticket per run. `remaining` empty with an
        existing rollup ticket still updates it (down to 0), reflecting
        the backlog actually shrinking; empty with no existing ticket is a
        no-op - nothing to create for a backlog that isn't there.
        """
        existing = self._find_by_label(ROLLUP_LABEL)
        if not remaining and existing is None:
            return None

        summary = self._build_rollup_summary(len(remaining))
        description = self._build_rollup_description(remaining)

        if existing is not None:
            self._update_ticket(existing, summary=summary, description=description)
            return existing

        payload: dict[str, Any] = {
            "fields": {
                "project": {"key": self.project_key},
                "summary": summary,
                "issuetype": {"name": "Task"},
                "labels": ["sonarqube", ROLLUP_LABEL],
                "description": description,
            }
        }
        response = requests.post(
            f"{self.base_url}/rest/api/3/issue",
            json=payload,
            auth=self.auth,
            headers=self.headers,
        )
        self._raise_for_status(response, "create rollup issue")
        return response.json()["key"]
