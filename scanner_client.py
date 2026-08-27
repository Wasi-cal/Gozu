"""
Generic scanner interface plus the SonarQube implementations.

Adding a new scanner (Snyk, Semgrep, whatever) means writing a new
ScannerClient subclass here and registering it in get_scanner_client() -
nothing in models.py, ticket_client.py, or the Temporal
workflow/activities/receiver needs to change.
"""

import logging
import os
from abc import ABC, abstractmethod

import requests

from models import Finding, Severity

logger = logging.getLogger(__name__)

# SonarQube's severity scale -> our normalized Severity. Anything not in
# here (including hotspots, which report a "vulnerability probability" of
# LOW/MEDIUM/HIGH instead of a real severity) falls back to MEDIUM with a
# logged warning.
SONAR_SEVERITY_MAP = {
    "BLOCKER": Severity.CRITICAL,
    "CRITICAL": Severity.CRITICAL,
    "MAJOR": Severity.HIGH,
    "MINOR": Severity.MEDIUM,
    "INFO": Severity.LOW,
}
DEFAULT_SEVERITY = Severity.MEDIUM

TYPE_LABELS = {
    "vulnerability": "Vulnerability",
    "hotspot": "Hotspot",
}


class ScannerClient(ABC):
    @abstractmethod
    def fetch_findings(self, project_key: str) -> list[Finding]:
        """
        Return every open finding (vulnerabilities, hotspots, or whatever
        categories this tool has) for a project, in normalized form.
        Combining multiple upstream categories into one list is an internal
        detail of each adapter, not part of the generic contract.
        """
        raise NotImplementedError


class SonarQubeServerClient(ScannerClient):
    """Real implementation for self-hosted SonarQube (Community Build, etc)."""

    def __init__(self, base_url: str, token: str):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self._rule_name_cache: dict[str, str] = {}

    def _auth(self):
        # SonarQube web API convention: token as HTTP basic auth username, empty password.
        return (self.token, "")

    def _fetch_rule_name(self, rule_key: str) -> str:
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

    def _map_severity(self, raw_severity: str | None) -> Severity:
        if raw_severity in SONAR_SEVERITY_MAP:
            return SONAR_SEVERITY_MAP[raw_severity]
        logger.warning(
            f"Unrecognized or missing severity '{raw_severity}', defaulting to '{DEFAULT_SEVERITY.value}'"
        )
        return DEFAULT_SEVERITY

    def _build_title(self, finding_type: str, rule_name: str, component: str, line: int | None, message: str) -> str:
        type_label = TYPE_LABELS.get(finding_type, finding_type)
        relative_path = component.split(":", 1)[-1]  # component is "{project_key}:{relative/path}"
        location = f"{relative_path}:{line}" if line is not None else relative_path
        message = " ".join(message.split())  # collapse newlines/extra whitespace
        return f"{type_label} [{rule_name}]: {message} ({location})"

    def _fetch_vulnerabilities(self, project_key: str) -> list[Finding]:
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

        findings = []
        for raw in response.json().get("issues", []):
            key = raw["key"]
            rule = raw.get("rule", "")
            rule_name = self._fetch_rule_name(rule) if rule else rule
            component = raw.get("component", "")
            line = raw.get("line")
            message = raw.get("message", "")
            findings.append(
                Finding(
                    key=key,
                    title=self._build_title("vulnerability", rule_name, component, line, message),
                    severity=self._map_severity(raw.get("severity")),
                    component=component,
                    line=line,
                    message=message,
                    finding_type="vulnerability",
                    deep_link=f"{self.base_url}/project/issues?id={project_key}&issues={key}",
                    source_tool="sonarqube",
                )
            )
        return findings

    def _fetch_hotspots(self, project_key: str) -> list[Finding]:
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

        findings = []
        for raw in response.json().get("hotspots", []):
            key = raw["key"]
            rule = raw.get("ruleKey", "")
            rule_name = self._fetch_rule_name(rule) if rule else rule
            component = raw.get("component", "")
            line = raw.get("line")
            message = raw.get("message", "")
            findings.append(
                Finding(
                    key=key,
                    title=self._build_title("hotspot", rule_name, component, line, message),
                    # hotspots don't carry a standard severity, only a "vulnerability
                    # probability" (LOW/MEDIUM/HIGH) - _map_severity falls back to
                    # MEDIUM with a warning since none of those match our scale.
                    severity=self._map_severity(raw.get("vulnerabilityProbability")),
                    component=component,
                    line=line,
                    message=message,
                    finding_type="hotspot",
                    deep_link=f"{self.base_url}/project/issues?id={project_key}&issues={key}",
                    source_tool="sonarqube",
                )
            )
        return findings

    def fetch_findings(self, project_key: str) -> list[Finding]:
        return self._fetch_vulnerabilities(project_key) + self._fetch_hotspots(project_key)


class SonarQubeCloudClient(ScannerClient):
    """
    Stub for SonarQube Cloud.

    Same constructor shape as SonarQubeServerClient, plus `organization`
    (SonarQube Cloud scopes everything under an organization). Intentionally
    unimplemented until we're ready to point this at the SaaS product - flip
    SCANNER_TYPE=sonarqube-cloud and implement this, nothing else in the
    codebase needs to change.
    """

    def __init__(self, base_url: str, token: str, organization: str):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.organization = organization

    def fetch_findings(self, project_key: str) -> list[Finding]:
        raise NotImplementedError(
            "SonarQube Cloud support comes later - see get_scanner_client()"
        )


def get_scanner_client() -> ScannerClient:
    """
    Reads SCANNER_TYPE from the environment and builds the matching
    ScannerClient. This is the ONLY place in the codebase that should know
    which concrete class is in use.

    Falls back to the old SONAR_MODE var ("local"/"cloud") if SCANNER_TYPE
    isn't set, so existing .env files keep working.
    """
    scanner_type = os.environ.get("SCANNER_TYPE")
    if not scanner_type:
        legacy_mode = os.environ.get("SONAR_MODE", "local")
        scanner_type = "sonarqube-cloud" if legacy_mode == "cloud" else "sonarqube"

    base_url = os.environ.get("SONAR_HOST_URL", "http://localhost:9000")
    token = os.environ.get("SONAR_TOKEN", "")

    if scanner_type == "sonarqube":
        return SonarQubeServerClient(base_url=base_url, token=token)
    elif scanner_type == "sonarqube-cloud":
        organization = os.environ.get("SONAR_ORGANIZATION", "")
        return SonarQubeCloudClient(base_url=base_url, token=token, organization=organization)
    else:
        raise ValueError(
            f"Unrecognized SCANNER_TYPE '{scanner_type}'. Expected 'sonarqube' or 'sonarqube-cloud'."
        )
