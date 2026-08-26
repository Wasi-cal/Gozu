"""Factory that picks the right SonarClient implementation based on SONAR_MODE."""

import os

from sonar.cloud_client import SonarQubeCloudClient
from sonar.interface import SonarClient
from sonar.server_client import SonarQubeServerClient


def get_sonar_client() -> SonarClient:
    """
    Reads SONAR_MODE from the environment and builds the matching
    SonarClient. This is the ONLY place in the codebase that should know
    which concrete class is in use.
    """
    mode = os.environ.get("SONAR_MODE", "local")
    base_url = os.environ.get("SONAR_HOST_URL", "http://localhost:9000")
    token = os.environ.get("SONAR_TOKEN", "")

    if mode == "local":
        return SonarQubeServerClient(base_url=base_url, token=token)
    elif mode == "cloud":
        organization = os.environ.get("SONAR_ORGANIZATION", "")
        return SonarQubeCloudClient(base_url=base_url, token=token, organization=organization)
    else:
        raise ValueError(
            f"Unrecognized SONAR_MODE '{mode}'. Expected 'local' or 'cloud'."
        )
