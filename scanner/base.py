# Copyright (c) 2026 Calfus Inc.
# Author: Wasiullah Rafeeq S
# Editor: Prakrit Mohanty

"""
Generic scanner interface plus shared constants. Adding a new scanner
(Snyk, Semgrep, whatever) means writing a new ScannerClient subclass and
registering it in scanner/factory.py - nothing in core/models.py,
ticket/, or the Temporal workflow/activities/receiver needs to change.
"""

from abc import ABC, abstractmethod
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel

from core.models import Finding, Severity


def resolve_container_host(url: str) -> str:
    """
    The worker always runs inside the Docker Compose network
    (docker-compose.yml); a config's `sonar_host_url` is a *host*-side
    address (wherever `gozu run` reaches SonarQube from), which
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
    ScannerClient to actually work - used by the CLI wizard to decide
    which Compose profiles/services to bring up and which host binaries
    to check for.
    """

    docker_services: list[str]
    host_dependencies: list[str]


class ScannerClient(ABC):
    @abstractmethod
    def fetch_findings(self, project_key: str, branch: str | None = None) -> list[Finding]:
        """
        Return every open finding (vulnerabilities, hotspots, or whatever
        categories this tool has) for a project, in normalized form.
        Combining multiple upstream categories into one list is an internal
        detail of each adapter, not part of the generic contract.

        `branch` scopes the query to a specific branch's analysis where the
        backend actually supports that (SonarQube Cloud, on every plan). A
        backend that doesn't distinguish branches (self-hosted Community
        Build, which only ever has one implicit analysis to begin with) is
        expected to pass it straight through to its query rather than
        special-case it away - a real mismatch should come back as "no
        findings", not be silently papered over.
        """
        raise NotImplementedError

    @abstractmethod
    def requirements(self) -> ScannerRequirements:
        """Docker services and host binaries this scanner needs to run."""
        raise NotImplementedError

    @abstractmethod
    def fetch_resolutions(self, finding_keys: list[str]) -> dict[str, str]:
        """
        Batch-check exactly these finding keys' current resolution in the
        scanner backend - not a full re-fetch of every finding, callers
        already know which keys they care about (see
        temporal/activities/reconcile_resolved_findings.py). Returns
        {key: resolution} only for keys that are now resolved; a key still
        open is simply absent from the result, not mapped to None.
        """
        raise NotImplementedError
