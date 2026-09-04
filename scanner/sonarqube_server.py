"""Real ScannerClient implementation for self-hosted SonarQube (Community Build, etc)."""

from core.models import Finding
from scanner.base import ScannerClient, ScannerRequirements, resolve_container_host
from scanner.sonarqube_common import SonarQubeIssueFetcher


class SonarQubeServerClient(SonarQubeIssueFetcher, ScannerClient):
    def __init__(self, base_url: str, token: str):
        super().__init__()
        self.base_url = base_url.rstrip("/")  # human-facing - used only for Finding.deep_link
        self._request_base_url = resolve_container_host(self.base_url)  # what this process actually calls
        self.token = token

    def fetch_findings(self, project_key: str, branch: str | None = None) -> list[Finding]:
        return self.fetch_sonarqube_findings(project_key, branch)

    def requirements(self) -> ScannerRequirements:
        return ScannerRequirements(docker_services=["sonarqube"], host_dependencies=["java"])
