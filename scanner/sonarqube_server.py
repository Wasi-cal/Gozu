# Copyright (c) 2026 Calfus Inc.
# Author: Wasiullah Rafeeq S
# Editor: Claude
#
# Depends on: SonarQube (SonarSource) - direct API client
# Depends on: scanner/base.py - resolve_container_host() for Docker container-host URL rewriting
# Depends on: scanner/sonarqube_common.py - SonarQubeIssueFetcher mixin for issue fetch/classification

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

    def fetch_resolutions(self, finding_keys: list[str]) -> dict[str, str]:
        return self.fetch_sonarqube_resolutions(finding_keys)
