"""Real SonarClient implementation for self-hosted SonarQube (Community Build, etc.)."""

import requests

from sonar.interface import SonarClient
from sonar.models import SonarIssue


class SonarQubeServerClient(SonarClient):
    def __init__(self, base_url: str, token: str):
        self.base_url = base_url.rstrip("/")
        self.token = token

    def _auth(self):
        # SonarQube web API convention: token as HTTP basic auth username, empty password.
        return (self.token, "")

    def fetch_vulnerabilities(self, project_key: str) -> list[SonarIssue]:
        url = f"{self.base_url}/api/issues/search"
        params = {
            "componentKeys": project_key,
            "types": "VULNERABILITY",
            "statuses": "OPEN,REOPENED",
        }
        response = requests.get(url, params=params, auth=self._auth())
        if response.status_code != 200:
            raise RuntimeError(
                f"SonarQube issues/search failed with status {response.status_code}: {response.text}"
            )

        data = response.json()
        issues = []
        for raw in data.get("issues", []):
            key = raw["key"]
            issues.append(
                SonarIssue(
                    key=key,
                    rule=raw.get("rule", ""),
                    severity=raw.get("severity", ""),
                    component=raw.get("component", ""),
                    line=raw.get("line"),
                    message=raw.get("message", ""),
                    type="VULNERABILITY",
                    deep_link=f"{self.base_url}/project/issues?id={project_key}&issues={key}",
                )
            )
        return issues

    def fetch_hotspots(self, project_key: str) -> list[SonarIssue]:
        url = f"{self.base_url}/api/hotspots/search"
        params = {
            "projectKey": project_key,
            "status": "TO_REVIEW",
        }
        response = requests.get(url, params=params, auth=self._auth())
        if response.status_code != 200:
            raise RuntimeError(
                f"SonarQube hotspots/search failed with status {response.status_code}: {response.text}"
            )

        data = response.json()
        hotspots = []
        for raw in data.get("hotspots", []):
            key = raw["key"]
            hotspots.append(
                SonarIssue(
                    key=key,
                    rule=raw.get("ruleKey", ""),
                    severity=raw.get("vulnerabilityProbability", ""),
                    component=raw.get("component", ""),
                    line=raw.get("line"),
                    message=raw.get("message", ""),
                    type="SECURITY_HOTSPOT",
                    deep_link=f"{self.base_url}/project/issues?id={project_key}&issues={key}",
                )
            )
        return hotspots
