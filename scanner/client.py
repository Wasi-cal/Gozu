"""
Generic scanner interface plus the SonarQube implementations.

Adding a new scanner (Snyk, Semgrep, whatever) means writing a new
ScannerClient subclass here and registering it in get_scanner_client() -
nothing in core/models.py, ticket/client.py, or the Temporal
workflow/activities/receiver needs to change.
"""

import os
from abc import ABC, abstractmethod
from urllib.parse import urlsplit, urlunsplit

import requests
from pydantic import BaseModel
from temporalio import activity

from core.models import Finding, Severity


def resolve_container_host(url: str) -> str:
    """
    The worker always runs inside the Docker Compose network
    (docker-compose.yml); a config's `sonar_host_url` is a *host*-side
    address (wherever `codescan run` reaches SonarQube from), which
    "localhost"/"127.0.0.1" never resolves to from inside the worker's
    own container. `host.docker.internal` does - natively on Docker
    Desktop, or via the "host.docker.internal:host-gateway" extra_hosts
    entry on Linux (see docker-compose.yml's worker service).

    Only for requests this process makes itself - a Finding's deep_link is
    deliberately left untouched (it's human-facing, shown in Jira ticket
    descriptions, and must stay resolvable from a browser outside Docker
    entirely) - see scanner/screenshot.py, which applies this separately
    to the URL it navigates to, for exactly that reason.
    """
    parts = urlsplit(url)
    if parts.hostname not in ("localhost", "127.0.0.1"):
        return url

    netloc = "host.docker.internal"
    if parts.port:
        netloc += f":{parts.port}"
    return urlunsplit(parts._replace(netloc=netloc))

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

# scanner_type -> display name. The single source of truth for "which
# scanners exist" - the init wizard's scanner-selection prompt reads this
# instead of hardcoding "sonarqube" as a magic string, so registering a
# future scanner here is enough to make it selectable.
SCANNER_REGISTRY: dict[str, str] = {
    "sonarqube": "SonarQube",
}


class ScannerRequirements(BaseModel):
    """
    What has to be true on the host/in Docker Compose for a given
    ScannerClient to actually work - used by the (Phase 2) CLI wizard to
    decide which Compose profiles/services to bring up and which host
    binaries to check for, not by anything in this phase.
    """

    docker_services: list[str]
    host_dependencies: list[str]


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

    @abstractmethod
    def requirements(self) -> ScannerRequirements:
        """Docker services and host binaries this scanner needs to run."""
        raise NotImplementedError


class SonarQubeServerClient(ScannerClient):
    """Real implementation for self-hosted SonarQube (Community Build, etc)."""

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
                f"{self._request_base_url}/api/rules/show",
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
        activity.logger.warning(
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
        url = f"{self._request_base_url}/api/issues/search"
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
        url = f"{self._request_base_url}/api/hotspots/search"
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

    def requirements(self) -> ScannerRequirements:
        return ScannerRequirements(docker_services=["sonarqube"], host_dependencies=["java"])


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

    def requirements(self) -> ScannerRequirements:
        # No local container - SonarQube Cloud is a hosted SaaS product -
        # but the sonar-scanner CLI still needs a JVM to run.
        return ScannerRequirements(docker_services=[], host_dependencies=["java"])


def build_scanner_client(scanner_type: str, scanner_mode: str, credentials: dict[str, str]) -> ScannerClient:
    """
    Pure constructor: build a ScannerClient from explicit params, no env
    reads. This is what a specific config's credentials (config_store.
    get_config(), as used by `codescan run`) go through; get_scanner_client()
    below is a thin env-reading wrapper around this for the legacy
    single-global-config path (the webhook receiver).
    """
    if scanner_type != "sonarqube":
        raise ValueError(f"Unrecognized scanner_type '{scanner_type}'. Expected 'sonarqube'.")

    token = credentials.get("sonar_token", "")
    if scanner_mode == "local":
        return SonarQubeServerClient(base_url=credentials.get("sonar_host_url", "http://localhost:9000"), token=token)
    if scanner_mode == "cloud":
        return SonarQubeCloudClient(
            base_url="https://sonarcloud.io", token=token, organization=credentials.get("sonar_organization", "")
        )
    raise ValueError(f"Unrecognized scanner_mode '{scanner_mode}'. Expected 'local' or 'cloud'.")


def get_scanner_client() -> ScannerClient:
    """
    Legacy path used by the webhook receiver, which has no per-config
    credentials of its own: reads SCANNER_TYPE/SONAR_* from the environment
    and delegates to build_scanner_client(). This is the only place in the
    codebase that should ever read those env vars for this purpose.

    Falls back to the old SONAR_MODE var ("local"/"cloud") if SCANNER_TYPE
    isn't set, so existing .env files keep working.
    """
    legacy_type = os.environ.get("SCANNER_TYPE")
    if legacy_type:
        scanner_mode = "cloud" if legacy_type == "sonarqube-cloud" else "local"
    else:
        scanner_mode = "cloud" if os.environ.get("SONAR_MODE") == "cloud" else "local"

    credentials = {
        "sonar_host_url": os.environ.get("SONAR_HOST_URL", "http://localhost:9000"),
        "sonar_token": os.environ.get("SONAR_TOKEN", ""),
        "sonar_organization": os.environ.get("SONAR_ORGANIZATION", ""),
    }
    return build_scanner_client("sonarqube", scanner_mode, credentials)
