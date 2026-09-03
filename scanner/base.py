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
