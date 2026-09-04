# Copyright (c) 2026 Calfus Inc.
# Author: Wasiullah Rafeeq S
#
# Depends on: Jira (Atlassian) - direct API client

"""
Active-sprint lookup + assignment for JiraClient - split out since it's a
self-contained concern (Agile board -> active sprint -> add issue), not
core issue CRUD.
"""

import requests
from temporalio import activity


class SprintAssigner:
    """Looks up a project's active sprint once per instance, then adds issues to it."""

    def __init__(self, base_url: str, auth: tuple[str, str], headers: dict[str, str], project_key: str):
        self.base_url = base_url
        self.auth = auth
        self.headers = headers
        self.project_key = project_key
        self._active_sprint_id: int | None = None
        self._looked_up = False

    def _raise_for_status(self, response: requests.Response, action: str) -> None:
        if not (200 <= response.status_code < 300):
            raise RuntimeError(f"Jira {action} failed with status {response.status_code}: {response.text}")

    def _active_sprint_id_lazy(self) -> int | None:
        if self._looked_up:
            return self._active_sprint_id
        self._looked_up = True

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
            activity.logger.warning(f"No active sprint on board {board_id}; new tickets will stay in the backlog")
            return None

        self._active_sprint_id = sprints[0]["id"]
        return self._active_sprint_id

    def add_issue(self, issue_key: str) -> None:
        sprint_id = self._active_sprint_id_lazy()
        if sprint_id is None:
            return

        response = requests.post(
            f"{self.base_url}/rest/agile/1.0/sprint/{sprint_id}/issue",
            json={"issues": [issue_key]},
            auth=self.auth,
            headers=self.headers,
        )
        self._raise_for_status(response, "add issue to sprint")
