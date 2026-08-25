"""
Minimal Jira Cloud REST API client used to create tickets for SonarQube
issues and to check whether a ticket already exists for a given issue
(dedupe).
"""

import logging

import requests

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

    def _raise_for_status(self, response: requests.Response, action: str):
        if not (200 <= response.status_code < 300):
            raise RuntimeError(
                f"Jira {action} failed with status {response.status_code}: {response.text}"
            )

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

    def _build_description_adf(self, issue: dict) -> dict:
        return {
            "type": "doc",
            "version": 1,
            "content": [
                {
                    "type": "paragraph",
                    "content": [
                        {"type": "text", "text": issue.get("message", "")}
                    ],
                },
                {
                    "type": "bulletList",
                    "content": [
                        {
                            "type": "listItem",
                            "content": [
                                {
                                    "type": "paragraph",
                                    "content": [
                                        {"type": "text", "text": f"Rule: {issue.get('rule', '')}"}
                                    ],
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
                                            "text": f"Component: {issue.get('component', '')}",
                                        }
                                    ],
                                }
                            ],
                        },
                        {
                            "type": "listItem",
                            "content": [
                                {
                                    "type": "paragraph",
                                    "content": [
                                        {"type": "text", "text": f"Line: {issue.get('line')}"}
                                    ],
                                }
                            ],
                        },
                        {
                            "type": "listItem",
                            "content": [
                                {
                                    "type": "paragraph",
                                    "content": [
                                        {"type": "text", "text": f"Type: {issue.get('type', '')}"}
                                    ],
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
                                            "text": issue.get("deep_link", ""),
                                            "marks": [
                                                {
                                                    "type": "link",
                                                    "attrs": {"href": issue.get("deep_link", "")},
                                                }
                                            ],
                                        }
                                    ],
                                }
                            ],
                        },
                    ],
                },
            ],
        }

    def create_ticket(self, issue: dict) -> str:
        """Create a Jira issue for a SonarQube issue dict, return the new issue key."""
        summary = f"[Sonar] {issue.get('rule', '')} in {issue.get('component', '')}:{issue.get('line')}"
        if len(summary) > SUMMARY_MAX_LENGTH:
            summary = summary[: SUMMARY_MAX_LENGTH - 3] + "..."

        payload = {
            "fields": {
                "project": {"key": self.project_key},
                "summary": summary,
                "issuetype": {"name": "Bug"},
                "priority": {"name": self._map_priority(issue.get("severity"))},
                "labels": ["sonarqube", "security", f"sonar-key-{issue['key']}"],
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

        return response.json()["key"]
