# Copyright (c) 2026 Calfus Inc.
# Author: Wasiullah Rafeeq S
# Editor: Prakrit Mohanty, Claude
#
# Depends on: scanner/sonarqube_cloud.py - dispatches to SonarQubeCloudClient for scanner_mode "cloud"
# Depends on: scanner/sonarqube_server.py - dispatches to SonarQubeServerClient for scanner_mode "local"

"""Pure-builder + env-reading-wrapper pair for constructing a ScannerClient - see ticket/factory.py for the same pattern."""

import os

from scanner.base import ScannerClient
from scanner.sonarqube_cloud import SonarQubeCloudClient
from scanner.sonarqube_server import SonarQubeServerClient


def build_scanner_client(scanner_type: str, scanner_mode: str, credentials: dict[str, str]) -> ScannerClient:
    """
    Pure constructor: build a ScannerClient from explicit params, no env
    reads. This is what a specific config's credentials (config.store.
    get_config(), as used by `gozu run`) go through; get_scanner_client()
    below is a thin env-reading wrapper around this for the legacy
    single-global-config path (the webhook receiver).
    """
    if scanner_type != "sonarqube":
        raise ValueError(f"Unrecognized scanner_type '{scanner_type}'. Expected 'sonarqube'.")

    token = credentials.get("sonar_token", "")
    if scanner_mode == "local":
        return SonarQubeServerClient(base_url=credentials.get("sonar_host_url", "http://localhost:9000"), token=token)
    if scanner_mode == "cloud":
        return SonarQubeCloudClient(
            base_url="https://sonarcloud.io", token=token, organization=credentials.get("sonar_organization", "")
        )
    raise ValueError(f"Unrecognized scanner_mode '{scanner_mode}'. Expected 'local' or 'cloud'.")


def get_scanner_client() -> ScannerClient:
    """
    Legacy path used by the webhook receiver, which has no per-config
    credentials of its own: reads SCANNER_TYPE/SONAR_* from the environment
    and delegates to build_scanner_client(). This is the only place in the
    codebase that should ever read those env vars for this purpose.

    Falls back to the old SONAR_MODE var ("local"/"cloud") if SCANNER_TYPE
    isn't set, so existing .env files keep working.
    """
    legacy_type = os.environ.get("SCANNER_TYPE")
    if legacy_type:
        scanner_mode = "cloud" if legacy_type == "sonarqube-cloud" else "local"
    else:
        scanner_mode = "cloud" if os.environ.get("SONAR_MODE") == "cloud" else "local"

    credentials = {
        "sonar_host_url": os.environ.get("SONAR_HOST_URL", "http://localhost:9000"),
        "sonar_token": os.environ.get("SONAR_TOKEN", ""),
        "sonar_organization": os.environ.get("SONAR_ORGANIZATION", ""),
    }
    return build_scanner_client("sonarqube", scanner_mode, credentials)
