# Copyright (c) 2026 Calfus Inc.
# Author: Wasiullah Rafeeq S
# Editor: Prakrit Mohanty
#
# Depends on: SonarQube (SonarSource) - direct API client

"""
ScannerClient implementation for SonarQube Cloud.

Host confirmed live (2026-09-03) against SonarQube Cloud's own web API:
sonarcloud.io is still correct for the v1 REST API used here - it did not
move when the product was renamed from SonarCloud.
"""

import time
from datetime import datetime

import requests

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

    def fetch_resolutions(self, finding_keys: list[str]) -> dict[str, str]:
        return self.fetch_sonarqube_resolutions(finding_keys)

    def wait_for_latest_analysis(
        self,
        project_key: str,
        branch: str,
        not_before: datetime,
        timeout: float = 300,
        poll_interval: float = 5,
    ) -> None:
        """
        For Automatic Analysis: there's no ceTaskId to poll (that only
        exists for a scan we ourselves triggered - see
        cli/scan_runner/scanner_exec.py's wait_for_analysis). Instead, poll
        api/project_analyses/search (results sorted newest-first) until its
        top entry's `date` is at or after `not_before`.

        `not_before` should be the commit's own push/authored timestamp,
        not "now" at the caller's own start - the caller's own startup
        (checkout, pip install, ...) takes real time, during which
        Automatic Analysis (triggered by the same push, in parallel) could
        already finish; comparing against the commit's timestamp instead
        avoids mistaking that as "not done yet" and timing out for no
        reason.
        """
        deadline = time.monotonic() + timeout

        while True:
            response = requests.get(
                f"{self._request_base_url}/api/project_analyses/search",
                params={"project": project_key, "branch": branch, "ps": 1, **self._extra_params()},
                auth=self._auth(),
            )
            if response.status_code != 200:
                raise RuntimeError(
                    f"SonarQube project_analyses/search failed with status {response.status_code}: {response.text}"
                )

            analyses = response.json().get("analyses", [])
            if analyses:
                analysis_date = datetime.fromisoformat(analyses[0]["date"])
                if analysis_date >= not_before:
                    return

            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"Timed out after {timeout}s waiting for a SonarQube Cloud analysis of "
                    f"'{project_key}' branch '{branch}' at or after {not_before.isoformat()}"
                )
            time.sleep(poll_interval)
