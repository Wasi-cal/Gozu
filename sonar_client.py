"""
Interface layer for talking to a SonarQube-flavored backend (self-hosted
SonarQube Community Build today, SonarQube Cloud later).

Nothing outside this module should know whether we're talking to a local
server or the cloud SaaS product. workflows.py / activities.py only ever
call get_sonar_client() and use the SonarClient interface it returns.
"""

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass

import requests


@dataclass
class SonarIssue:
    key: str
    rule: str
    severity: str
    component: str
    line: int | None
    message: str
    type: str  # "VULNERABILITY" or "SECURITY_HOTSPOT"
    deep_link: str


class SonarClient(ABC):
    """Abstract interface every Sonar backend (server or cloud) must implement."""

    @abstractmethod
    def fetch_vulnerabilities(self, project_key: str) -> list[SonarIssue]:
        """Return open/reopened VULNERABILITY issues for a project."""
        raise NotImplementedError

    @abstractmethod
    def fetch_hotspots(self, project_key: str) -> list[SonarIssue]:
        """Return TO_REVIEW security hotspots for a project."""
        raise NotImplementedError


class SonarQubeServerClient(SonarClient):
    """Real implementation for self-hosted SonarQube (Community Build, etc.)."""

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


class SonarQubeCloudClient(SonarClient):
    """
    Stub for SonarQube Cloud support.

    Same constructor shape as SonarQubeServerClient, plus `organization`
    (SonarQube Cloud scopes everything under an organization). Intentionally
    unimplemented until we're ready to point this at the SaaS product -
    flip SONAR_MODE=cloud and implement these two methods, nothing else
    in the codebase needs to change.
    """

    def __init__(self, base_url: str, token: str, organization: str):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.organization = organization

    def fetch_vulnerabilities(self, project_key: str) -> list[SonarIssue]:
        raise NotImplementedError(
            "SonarQube Cloud support comes later - see get_sonar_client()"
        )

    def fetch_hotspots(self, project_key: str) -> list[SonarIssue]:
        raise NotImplementedError(
            "SonarQube Cloud support comes later - see get_sonar_client()"
        )


def get_sonar_client() -> SonarClient:
    """
    Factory that reads SONAR_MODE from the environment and builds the
    matching SonarClient. This is the ONLY place in the codebase that
    should know which concrete class is in use.
    """
    mode = os.environ.get("SONAR_MODE", "local")
    base_url = os.environ.get("SONAR_HOST_URL", "http://localhost:9000")
    token = os.environ.get("SONAR_TOKEN", "")

    if mode == "local":
        return SonarQubeServerClient(base_url=base_url, token=token)
    elif mode == "cloud":
        organization = os.environ.get("SONAR_ORGANIZATION", "")
        return SonarQubeCloudClient(base_url=base_url, token=token, organization=organization)
    else:
        raise ValueError(
            f"Unrecognized SONAR_MODE '{mode}'. Expected 'local' or 'cloud'."
        )
