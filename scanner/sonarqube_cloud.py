# Copyright (c) 2026 Calfus Inc.
# Author: Wasiullah Rafeeq S
#
# Depends on: SonarQube (SonarSource) - direct API client

"""
ScannerClient implementation for SonarQube Cloud.

Host confirmed live (2026-09-03) against SonarQube Cloud's own web API:
sonarcloud.io is still correct for the v1 REST API used here - it did not
move when the product was renamed from SonarCloud.
"""

from core.models import Finding
from scanner.base import ScannerClient, ScannerRequirements
from scanner.sonarqube_common import SonarQubeIssueFetcher


class SonarQubeCloudClient(SonarQubeIssueFetcher, ScannerClient):
    def __init__(self, base_url: str, token: str, organization: str):
        super().__init__()
        self.base_url = base_url.rstrip("/")
        self._request_base_url = self.base_url  # always a public SaaS host - no container-host rewriting needed
        self.token = token
        self.organization = organization

    def _extra_params(self) -> dict[str, str]:
        # Required by SonarQube Cloud on every request that isn't already
        # scoped by componentKeys alone (confirmed live: api/rules/show
        # rejects a Cloud call without it).
        return {"organization": self.organization}

    def fetch_findings(self, project_key: str, branch: str | None = None) -> list[Finding]:
        return self.fetch_sonarqube_findings(project_key, branch)

    def requirements(self) -> ScannerRequirements:
        # No local container - SonarQube Cloud is a hosted SaaS product -
        # but the sonar-scanner CLI still needs a JVM to run.
        return ScannerRequirements(docker_services=[], host_dependencies=["java"])
