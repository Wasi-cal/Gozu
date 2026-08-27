"""Real SonarClient implementation for self-hosted SonarQube (Community Build, etc.)."""

import requests

from sonar.interface import SonarClient
from sonar.models import SonarIssue


class SonarQubeServerClient(SonarClient):
    def __init__(self, base_url: str, token: str):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self._rule_name_cache: dict[str, str] = {}

    def _auth(self):
        # SonarQube web API convention: token as HTTP basic auth username, empty password.
        return (self.token, "")

    def fetch_rule_name(self, rule_key: str) -> str:
        if rule_key in self._rule_name_cache:
            return self._rule_name_cache[rule_key]

        name = rule_key
        try:
            response = requests.get(
                f"{self.base_url}/api/rules/show",
                params={"key": rule_key},
                auth=self._auth(),
            )
            if response.status_code == 200:
                name = response.json().get("rule", {}).get("name", rule_key)
        except requests.RequestException:
            pass  # fall back to the rule key rather than fail the whole fetch

        self._rule_name_cache[rule_key] = name
        return name

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
            rule = raw.get("rule", "")
            issues.append(
                SonarIssue(
                    key=key,
                    rule=rule,
                    rule_name=self.fetch_rule_name(rule) if rule else "",
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
            rule = raw.get("ruleKey", "")
            hotspots.append(
                SonarIssue(
                    key=key,
                    rule=rule,
                    rule_name=self.fetch_rule_name(rule) if rule else "",
                    severity=raw.get("vulnerabilityProbability", ""),
                    component=raw.get("component", ""),
                    line=raw.get("line"),
                    message=raw.get("message", ""),
                    type="SECURITY_HOTSPOT",
                    deep_link=f"{self.base_url}/project/issues?id={project_key}&issues={key}",
                )
            )
        return hotspots
