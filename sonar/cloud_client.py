"""
Stub SonarClient for SonarQube Cloud.

Same constructor shape as SonarQubeServerClient, plus `organization`
(SonarQube Cloud scopes everything under an organization). Intentionally
unimplemented until we're ready to point this at the SaaS product - flip
SONAR_MODE=cloud and implement these two methods, nothing else in the
codebase needs to change.
"""

from sonar.interface import SonarClient
from sonar.models import SonarIssue


class SonarQubeCloudClient(SonarClient):
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

    def fetch_rule_name(self, rule_key: str) -> str:
        raise NotImplementedError(
            "SonarQube Cloud support comes later - see get_sonar_client()"
        )
