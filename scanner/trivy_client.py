# Copyright (c) 2026 Calfus Inc.
# Author: Wasiullah Rafeeq S
#
# Depends on: Trivy (Aqua Security) - direct CLI invocation

"""
ScannerClient implementation for Trivy `fs` mode (dependency/CVE scanning
of a filesystem path) - see scanner/base.py's ScannerClient for the
contract every scanner implements.

Fundamentally different shape from the SonarQube clients
(scanner/sonarqube_common.py): `trivy fs` is a single synchronous
subprocess call that returns everything in one shot - there is no
server-side analysis to trigger and poll, so this client deliberately
does NOT reuse any of SonarQube's async trigger/poll machinery. Invoke,
parse, return - that's the whole client.

`project_key` (ScannerClient.fetch_findings()'s first parameter) means
something different here than it does for SonarQube: it's the filesystem
path to scan, not a remote project identifier - see that method's own
docstring, widened to say a scanner defines what this parameter means for
its own fetch_findings() docstring, once a second scanner existed to make
that worth saying explicitly.

`branch` is accepted (it's part of the ScannerClient contract) but always
ignored - `trivy fs` has no branch concept at all: it scans whatever is
actually on disk at `project_key` right now, there is no server-side
per-branch analysis the way SonarQube Cloud has. A caller wanting a
specific branch's dependencies scanned is responsible for having that
branch checked out at `project_key` before calling this - the same
"caller's job, not this client's" split SonarQube's own local/Community
path already has for its one implicit branch.
"""

import json
import subprocess

from core.errors import ScannerExecutionError
from core.models import Finding, Severity
from scanner.base import ScannerClient, ScannerRequirements

# Trivy's severity scale -> our normalized Severity. UNKNOWN is Trivy's
# own defined value (means "not yet CVSS-scored", not "malformed/missing")
# - deliberately mapped to MEDIUM, not LOW: an unscored CVE could turn out
# to be CRITICAL once scored, and the cost of a slightly-too-prominent
# ticket is far smaller than the cost of a real critical sitting
# unnoticed in a LOW-priority backlog. Confirmed as the intended direction
# in review, not a default picked without discussion.
TRIVY_SEVERITY_MAP = {
    "CRITICAL": Severity.CRITICAL,
    "HIGH": Severity.HIGH,
    "MEDIUM": Severity.MEDIUM,
    "LOW": Severity.LOW,
    "UNKNOWN": Severity.MEDIUM,
}
DEFAULT_TRIVY_SEVERITY = Severity.MEDIUM


def _map_trivy_severity(raw_severity: str | None) -> Severity:
    if raw_severity in TRIVY_SEVERITY_MAP:
        return TRIVY_SEVERITY_MAP[raw_severity]
    return DEFAULT_TRIVY_SEVERITY


def _build_finding(vuln: dict, target: str) -> Finding:
    """
    One Trivy `Vulnerabilities[]` entry -> one normalized Finding. `key` is
    a composite fingerprint (VulnerabilityID|PkgName|Target), not a single
    server-issued id the way SonarQube's `key` is - Trivy has no
    persistent server-side issue identity at all, so this client
    constructs its own, scoped narrowly enough that the same CVE in two
    different packages (or the same package at two different paths/lock
    files) gets two distinct claims-ledger entries rather than colliding.
    ticket/claims.py's (destination, finding_key) primary key needs no
    schema change to support this - finding_key was always an opaque
    string, never parsed or validated against a fixed shape.
    """
    vulnerability_id = vuln.get("VulnerabilityID", "")
    pkg_name = vuln.get("PkgName", "")
    installed_version = vuln.get("InstalledVersion")
    # Trivy reports an unfixed vulnerability as an empty string, not an
    # absent key - normalized to None here so "no fix yet" is a single,
    # meaningful state (checked with `is None`) rather than two different
    # falsy representations a caller has to know to treat the same.
    fixed_version = vuln.get("FixedVersion") or None
    title = vuln.get("Title") or vulnerability_id

    return Finding(
        key=f"{vulnerability_id}|{pkg_name}|{target}",
        title=f"{vulnerability_id}: {title} ({pkg_name})",
        severity=_map_trivy_severity(vuln.get("Severity")),
        component=target,
        line=None,
        message=vuln.get("Description") or title,
        finding_type="vulnerability",
        # Trivy's own PrimaryURL is the exact reference for THIS CVE
        # (NVD, a GitHub advisory, or a vendor advisory, depending on
        # where Trivy's DB sourced it) - preferred over constructing our
        # own NVD link, which would be wrong for a non-NVD-sourced advisory
        # and redundant when Trivy already hands back the right one.
        deep_link=vuln.get("PrimaryURL") or f"https://nvd.nist.gov/vuln/detail/{vulnerability_id}",
        source_tool="trivy",
        package_name=pkg_name,
        installed_version=installed_version,
        fixed_version=fixed_version,
    )


class TrivyClient(ScannerClient):
    def requirements(self) -> ScannerRequirements:
        return ScannerRequirements(docker_services=[], host_dependencies=["trivy"])

    def fetch_findings(self, project_key: str, branch: str | None = None) -> list[Finding]:
        # No --exit-code flag - Trivy's default (unset) exits 0 even when
        # vulnerabilities are found, reserving a non-zero exit for a
        # genuine scan failure. Explicitly NOT relying on --exit-code's
        # "exit non-zero if X severity found" behavior here, which would
        # make "vulnerabilities exist" indistinguishable from "the scan
        # itself failed" - exactly the ambiguity this client must not
        # introduce.
        try:
            result = subprocess.run(
                ["trivy", "fs", "--format", "json", "--quiet", project_key],
                capture_output=True,
                text=True,
            )
        except FileNotFoundError as e:
            raise ScannerExecutionError(
                "The `trivy` binary was not found on PATH - install it or check "
                "cli/prerequisites' Trivy setup step."
            ) from e

        if result.returncode != 0:
            raise ScannerExecutionError(
                f"trivy fs exited with status {result.returncode}: {result.stderr.strip()}"
            )

        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError as e:
            raise ScannerExecutionError(f"trivy fs produced unparseable output: {e}") from e

        findings: list[Finding] = []
        for entry in data.get("Results") or []:
            target = entry.get("Target", project_key)
            for vuln in entry.get("Vulnerabilities") or []:
                findings.append(_build_finding(vuln, target))
        return findings

    def fetch_resolutions(self, finding_keys: list[str]) -> dict[str, str]:
        """
        Always empty - a known v1 gap, not an oversight. Unlike
        SonarQube's issues/search (a cheap, targeted server-side query by
        exact issue keys), Trivy has no server-side state to query at
        all: the only way to know whether a given
        VulnerabilityID|PkgName|Target still exists is to re-run `trivy
        fs` against the target again, which costs exactly as much as a
        full fetch_findings() call - and reconcile_resolved_findings_activity
        runs independently of (and in addition to) fetch_findings_activity
        every cycle, so a "real" implementation here would scan twice per
        cycle for every Trivy-backed config.

        Net effect: a Trivy-sourced ticket is never auto-closed by gozu,
        even after the underlying package is upgraded - closing it
        requires either noticing the CVE is gone and doing it manually,
        or (a real future improvement, not done here) restructuring
        reconciliation to diff against fetch_findings_activity's own
        fresh results each cycle instead of querying the scanner
        independently, which would need no extra scanning at all but
        touches shared workflow sequencing used by every scanner, not
        just this one.
        """
        return {}
