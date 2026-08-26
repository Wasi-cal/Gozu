"""Activity: fetch vulnerabilities + security hotspots for a project via the Sonar interface."""

from temporalio import activity

from sonar.factory import get_sonar_client
from sonar.models import SonarIssue


@activity.defn
async def fetch_vulnerabilities_activity(project_key: str) -> list[SonarIssue]:
    client = get_sonar_client()

    vulnerabilities = client.fetch_vulnerabilities(project_key)
    hotspots = client.fetch_hotspots(project_key)

    return vulnerabilities + hotspots
