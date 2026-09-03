"""
Stub ScannerClient for SonarQube Cloud.

Same constructor shape as SonarQubeServerClient, plus `organization`
(SonarQube Cloud scopes everything under an organization). Intentionally
unimplemented until we're ready to point this at the SaaS product's API -
flip scanner_mode to "cloud" and implement fetch_findings(), nothing else
in the codebase needs to change.
"""

from core.models import Finding
from scanner.base import ScannerClient, ScannerRequirements


class SonarQubeCloudClient(ScannerClient):
    def __init__(self, base_url: str, token: str, organization: str):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.organization = organization

    def fetch_findings(self, project_key: str) -> list[Finding]:
        raise NotImplementedError("SonarQube Cloud support comes later - see scanner/factory.py")

    def requirements(self) -> ScannerRequirements:
        # No local container - SonarQube Cloud is a hosted SaaS product -
        # but the sonar-scanner CLI still needs a JVM to run.
        return ScannerRequirements(docker_services=[], host_dependencies=["java"])
