"""Abstract interface every Sonar backend (server or cloud) must implement."""

from abc import ABC, abstractmethod

from sonar.models import SonarIssue


class SonarClient(ABC):
    @abstractmethod
    def fetch_vulnerabilities(self, project_key: str) -> list[SonarIssue]:
        """Return open/reopened VULNERABILITY issues for a project."""
        raise NotImplementedError

    @abstractmethod
    def fetch_hotspots(self, project_key: str) -> list[SonarIssue]:
        """Return TO_REVIEW security hotspots for a project."""
        raise NotImplementedError
