# Copyright (c) 2026 Calfus Inc.
# Author: Prakrit Mohanty

import pytest

from scanner.factory import build_scanner_client
from scanner.semgrep_local import SemgrepLocalClient
from scanner.sonarqube_cloud import SonarQubeCloudClient
from scanner.sonarqube_server import SonarQubeServerClient


def test_sonarqube_local_dispatches_to_server_client():
    assert isinstance(build_scanner_client("sonarqube", "local", {}), SonarQubeServerClient)


def test_sonarqube_cloud_dispatches_to_cloud_client():
    assert isinstance(build_scanner_client("sonarqube", "cloud", {}), SonarQubeCloudClient)


def test_sonarqube_unrecognized_mode_raises():
    with pytest.raises(ValueError, match="scanner_mode"):
        build_scanner_client("sonarqube", "platform", {})


def test_semgrep_local_dispatches_to_semgrep_local_client():
    assert isinstance(build_scanner_client("semgrep", "local", {}), SemgrepLocalClient)


def test_semgrep_unrecognized_mode_raises():
    with pytest.raises(ValueError, match="scanner_mode"):
        build_scanner_client("semgrep", "platform", {})


def test_unrecognized_scanner_type_raises():
    with pytest.raises(ValueError, match="scanner_type"):
        build_scanner_client("snyk", "local", {})
