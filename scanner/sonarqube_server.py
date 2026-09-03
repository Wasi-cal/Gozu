"""Real ScannerClient implementation for self-hosted SonarQube (Community Build, etc)."""

import requests
from temporalio import activity

from core.models import Finding, Severity
from scanner.base import (
    DEFAULT_SEVERITY,
    SONAR_SEVERITY_MAP,
    ScannerClient,
    ScannerRequirements,
    resolve_container_host,
)


class SonarQubeServerClient(ScannerClient):
    def __init__(self, base_url: str, token: str):
        self.base_url = base_url.rstrip("/")  # human-facing - used only for Finding.deep_link
        self._request_base_url = resolve_container_host(self.base_url)  # what this process actually calls
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
                f"{self._request_base_url}/api/rules/show", params={"key": rule_key}, auth=self._auth()
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
        activity.logger.warning(
            f"Unrecognized or missing severity '{raw_severity}', defaulting to '{DEFAULT_SEVERITY.value}'"
        )
        return DEFAULT_SEVERITY

    def _build_title(self, rule_name: str, component: str, line: int | None) -> str:
        """
        The rule's own display name already reads as a clean human sentence
        (e.g. "CSRF protections should not be disabled") - just append
        where it was found. Deliberately drops the raw issue message and
        finding-type label that used to be crammed into the title; both
        still show up in the ticket description.
        """
        relative_path = component.split(":", 1)[-1]  # component is "{project_key}:{relative/path}"
        location = f"{relative_path}:{line}" if line is not None else relative_path
        return f"{rule_name} ({location})"

    def _fetch(self, endpoint: str, params: dict, list_key: str, rule_field: str, finding_type: str) -> list[Finding]:
        response = requests.get(f"{self._request_base_url}{endpoint}", params=params, auth=self._auth())
        if response.status_code != 200:
            raise RuntimeError(f"SonarQube {endpoint} failed with status {response.status_code}: {response.text}")

        findings = []
        for raw in response.json().get(list_key, []):
            key = raw["key"]
            rule = raw.get(rule_field, "")
            rule_name = self._fetch_rule_name(rule) if rule else rule
            component = raw.get("component", "")
            line = raw.get("line")
            message = raw.get("message", "")
            severity_field = "vulnerabilityProbability" if finding_type == "hotspot" else "severity"
            findings.append(
                Finding(
                    key=key,
                    title=self._build_title(rule_name, component, line),
                    severity=self._map_severity(raw.get(severity_field)),
                    component=component,
                    line=line,
                    message=message,
                    finding_type=finding_type,
                    deep_link=f"{self.base_url}/project/issues?id={params.get('componentKeys') or params.get('projectKey')}&issues={key}",
                    source_tool="sonarqube",
                )
            )
        return findings

    def fetch_findings(self, project_key: str) -> list[Finding]:
        vulnerabilities = self._fetch(
            "/api/issues/search",
            {"componentKeys": project_key, "types": "VULNERABILITY", "statuses": "OPEN,REOPENED"},
            list_key="issues",
            rule_field="rule",
            finding_type="vulnerability",
        )
        # hotspots don't carry a standard severity, only a "vulnerability
        # probability" (LOW/MEDIUM/HIGH) - _fetch's severity_field handles
        # that, and _map_severity falls back to MEDIUM with a warning since
        # none of those match our scale.
        hotspots = self._fetch(
            "/api/hotspots/search",
            {"projectKey": project_key, "status": "TO_REVIEW"},
            list_key="hotspots",
            rule_field="ruleKey",
            finding_type="hotspot",
        )
        return vulnerabilities + hotspots

    def requirements(self) -> ScannerRequirements:
        return ScannerRequirements(docker_services=["sonarqube"], host_dependencies=["java"])
