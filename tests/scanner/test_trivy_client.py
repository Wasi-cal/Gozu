# Copyright (c) 2026 Calfus Inc.
# Author: Wasiullah Rafeeq S

import json
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from core.errors import ScannerExecutionError
from core.models import Severity
from scanner.trivy_client import DEFAULT_TRIVY_SEVERITY, TRIVY_SEVERITY_MAP, TrivyClient, _map_trivy_severity

# A trimmed extract of a REAL `trivy fs --format json` run (Trivy 0.74.0)
# against requirements.txt pinning flask==0.12 and requests==2.6.0 -
# captured live, not a hand-typed guess at the schema. Kept to the fields
# TrivyClient actually reads plus a couple of real vendor-specific ones
# (VendorIDs, CVSS, etc.) it deliberately ignores, to prove parsing
# doesn't choke on the extra fields real Trivy output always carries.
REAL_TRIVY_OUTPUT = {
    "SchemaVersion": 2,
    "ArtifactName": ".",
    "ArtifactType": "filesystem",
    "Results": [
        {
            "Target": "requirements.txt",
            "Class": "lang-pkgs",
            "Type": "pip",
            "Vulnerabilities": [
                {
                    "VulnerabilityID": "CVE-2018-1000656",
                    "VendorIDs": ["GHSA-562c-5r94-xh97"],
                    "PkgName": "flask",
                    "InstalledVersion": "0.12",
                    "FixedVersion": "0.12.3",
                    "Severity": "HIGH",
                    "PrimaryURL": "https://avd.aquasec.com/nvd/cve-2018-1000656",
                    "Title": "python-flask: Denial of Service via crafted JSON file",
                    "Description": "The Pallets Project flask version Before 0.12.3 contains a CWE-20...",
                },
                {
                    # A real entry with no FixedVersion at all yet (empty
                    # string, not an absent key - confirmed live) and no
                    # Title (falls back to the bare VulnerabilityID).
                    "VulnerabilityID": "CVE-2026-27205",
                    "PkgName": "flask",
                    "InstalledVersion": "0.12",
                    "FixedVersion": "",
                    "Severity": "LOW",
                    "PrimaryURL": "https://avd.aquasec.com/nvd/cve-2026-27205",
                },
                {
                    # Confirmed live: Trivy can report SEVERAL fixed
                    # versions as one comma-separated string, not a list -
                    # this client stores it verbatim as display text,
                    # never parses/splits it.
                    "VulnerabilityID": "CVE-2023-30861",
                    "PkgName": "flask",
                    "InstalledVersion": "0.12",
                    "FixedVersion": "2.3.2, 2.2.5",
                    "Severity": "HIGH",
                    "Title": "flask: possible disclosure of permanent session cookie",
                },
            ],
        },
        {
            # A real Target can appear with no Vulnerabilities key at all
            # (a lockfile Trivy scanned but found nothing in) - must not
            # crash, must contribute zero findings.
            "Target": "poetry.lock",
            "Class": "lang-pkgs",
            "Type": "poetry",
        },
    ],
}


def make_result(returncode: int = 0, stdout: str = "", stderr: str = "") -> MagicMock:
    return MagicMock(returncode=returncode, stdout=stdout, stderr=stderr)


# --- Severity mapping ---


def test_trivy_severity_map_covers_every_documented_value():
    assert TRIVY_SEVERITY_MAP["CRITICAL"] == Severity.CRITICAL
    assert TRIVY_SEVERITY_MAP["HIGH"] == Severity.HIGH
    assert TRIVY_SEVERITY_MAP["MEDIUM"] == Severity.MEDIUM
    assert TRIVY_SEVERITY_MAP["LOW"] == Severity.LOW


def test_unknown_severity_maps_to_medium_not_low():
    """
    Confirmed as the deliberate direction in review: Trivy's UNKNOWN means
    "not yet CVSS-scored", not "confirmed low-impact" - mapping it to LOW
    would under-react to something that could turn out CRITICAL once
    scored, so it maps to MEDIUM instead, same as DEFAULT_TRIVY_SEVERITY.
    """
    assert _map_trivy_severity("UNKNOWN") == Severity.MEDIUM
    assert TRIVY_SEVERITY_MAP["UNKNOWN"] == DEFAULT_TRIVY_SEVERITY


def test_unrecognized_severity_string_falls_back_to_default():
    assert _map_trivy_severity("SOMETHING_NEW_TRIVY_ADDED_LATER") == DEFAULT_TRIVY_SEVERITY


def test_missing_severity_falls_back_to_default():
    assert _map_trivy_severity(None) == DEFAULT_TRIVY_SEVERITY


# --- Parsing real captured output ---


def test_parses_every_vulnerability_across_all_targets():
    with patch("subprocess.run", return_value=make_result(stdout=json.dumps(REAL_TRIVY_OUTPUT))):
        findings = TrivyClient().fetch_findings("/some/repo")

    assert len(findings) == 3  # poetry.lock's target contributes zero
    assert {f.key.split("|")[0] for f in findings} == {"CVE-2018-1000656", "CVE-2026-27205", "CVE-2023-30861"}


def test_dedup_fingerprint_is_vulnerability_id_pkg_name_target():
    """
    VulnerabilityID + PkgName + Target, not SonarQube's rule+file+line -
    see ticket/claims.py, which treats finding_key as an opaque string
    either way (no schema change needed for this to just work).
    """
    with patch("subprocess.run", return_value=make_result(stdout=json.dumps(REAL_TRIVY_OUTPUT))):
        findings = TrivyClient().fetch_findings("/some/repo")

    flask_finding = next(f for f in findings if f.key.startswith("CVE-2018-1000656"))
    assert flask_finding.key == "CVE-2018-1000656|flask|requirements.txt"


def test_empty_fixed_version_normalizes_to_none_not_empty_string():
    with patch("subprocess.run", return_value=make_result(stdout=json.dumps(REAL_TRIVY_OUTPUT))):
        findings = TrivyClient().fetch_findings("/some/repo")

    unfixed = next(f for f in findings if f.key.startswith("CVE-2026-27205"))
    assert unfixed.fixed_version is None
    assert unfixed.severity == Severity.LOW


def test_multi_version_fixed_version_string_kept_verbatim():
    with patch("subprocess.run", return_value=make_result(stdout=json.dumps(REAL_TRIVY_OUTPUT))):
        findings = TrivyClient().fetch_findings("/some/repo")

    multi = next(f for f in findings if f.key.startswith("CVE-2023-30861"))
    assert multi.fixed_version == "2.3.2, 2.2.5"


def test_finding_has_no_file_or_line_location():
    with patch("subprocess.run", return_value=make_result(stdout=json.dumps(REAL_TRIVY_OUTPUT))):
        findings = TrivyClient().fetch_findings("/some/repo")

    assert all(f.line is None for f in findings)
    assert all(f.component == "requirements.txt" for f in findings)


def test_deep_link_prefers_primary_url_when_present():
    with patch("subprocess.run", return_value=make_result(stdout=json.dumps(REAL_TRIVY_OUTPUT))):
        findings = TrivyClient().fetch_findings("/some/repo")

    flask_finding = next(f for f in findings if f.key.startswith("CVE-2018-1000656"))
    assert flask_finding.deep_link == "https://avd.aquasec.com/nvd/cve-2018-1000656"


def test_deep_link_falls_back_to_nvd_when_no_primary_url():
    payload = {
        "Results": [
            {
                "Target": "Pipfile.lock",
                "Vulnerabilities": [
                    {"VulnerabilityID": "CVE-2099-00001", "PkgName": "pkg", "InstalledVersion": "1.0", "Severity": "HIGH"}
                ],
            }
        ]
    }
    with patch("subprocess.run", return_value=make_result(stdout=json.dumps(payload))):
        findings = TrivyClient().fetch_findings("/some/repo")

    assert findings[0].deep_link == "https://nvd.nist.gov/vuln/detail/CVE-2099-00001"


# --- Edge cases: empty findings, absent keys ---


def test_no_vulnerabilities_anywhere_returns_empty_list():
    payload = {"Results": [{"Target": "requirements.txt", "Class": "lang-pkgs", "Type": "pip"}]}
    with patch("subprocess.run", return_value=make_result(stdout=json.dumps(payload))):
        assert TrivyClient().fetch_findings("/clean/repo") == []


def test_no_results_key_at_all_returns_empty_list():
    with patch("subprocess.run", return_value=make_result(stdout=json.dumps({}))):
        assert TrivyClient().fetch_findings("/clean/repo") == []


# --- Subprocess error handling ---


def test_missing_binary_raises_scanner_execution_error():
    with patch("subprocess.run", side_effect=FileNotFoundError()):
        with pytest.raises(ScannerExecutionError, match="not found on PATH"):
            TrivyClient().fetch_findings("/some/repo")


def test_nonzero_exit_raises_scanner_execution_error_with_stderr():
    with patch("subprocess.run", return_value=make_result(returncode=1, stderr="unknown flag: --bogus")):
        with pytest.raises(ScannerExecutionError, match="unknown flag"):
            TrivyClient().fetch_findings("/some/repo")


def test_malformed_json_raises_scanner_execution_error():
    with patch("subprocess.run", return_value=make_result(stdout="not valid json{{{")):
        with pytest.raises(ScannerExecutionError, match="unparseable"):
            TrivyClient().fetch_findings("/some/repo")


def test_does_not_pass_exit_code_flag():
    """
    Deliberately never relies on --exit-code's "exit non-zero if X
    severity found" behavior - that would make "vulnerabilities exist"
    indistinguishable from "the scan itself failed".
    """
    with patch("subprocess.run", return_value=make_result(stdout="{}")) as mock_run:
        TrivyClient().fetch_findings("/some/repo")

    args = mock_run.call_args.args[0]
    assert "--exit-code" not in args


# --- Interface contract ---


def test_fetch_resolutions_always_empty():
    """
    Known v1 gap, not an oversight - see TrivyClient.fetch_resolutions()'s
    own docstring for why a "real" implementation would cost as much as a
    full rescan, run twice per cycle. Confirmed as the agreed v1 direction.
    """
    assert TrivyClient().fetch_resolutions(["a", "b", "c"]) == {}


def test_requirements_declares_trivy_host_dependency():
    requirements = TrivyClient().requirements()
    assert requirements.docker_services == []
    assert "trivy" in requirements.host_dependencies
