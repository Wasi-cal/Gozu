# Copyright (c) 2026 Calfus Inc.
# Author: Prakrit Mohanty
#
# Depends on: scanner/base.py - ScannerClient/ScannerRequirements/DEFAULT_SEVERITY

"""
Real ScannerClient implementation for the local, open-source `semgrep` CLI
(no semgrep.dev account/API involved).

Unlike SonarQube's ScannerClient implementations, this one doesn't query a
remote server for results computed elsewhere - Semgrep OSS has no server.
`fetch_findings()` runs the actual scan itself, synchronously, against
whatever path it's given. This is always invoked host-side
(cli/scan_runner/semgrep_exec.py), never from inside the Temporal worker -
the worker container has no access to the user's checkout, so its findings
are threaded straight into the workflow input instead (see
temporal/activities/fetch_findings.py's `pre_fetched_findings` path).
"""

import hashlib
import json
import subprocess

from core.models import Finding, Severity
from scanner.base import DEFAULT_SEVERITY, ScannerClient, ScannerRequirements

# Confirmed live against semgrep-interfaces' semgrep_output_v1.jsonschema:
# `extra.severity` covers both the classic ERROR/WARNING/INFO scale and the
# newer CRITICAL/HIGH/MEDIUM/LOW scale some rules report, plus two
# non-actionable rule kinds (EXPERIMENT/INVENTORY, informational only).
SEMGREP_SEVERITY_MAP = {
    "CRITICAL": Severity.CRITICAL,
    "ERROR": Severity.HIGH,
    "HIGH": Severity.HIGH,
    "WARNING": Severity.MEDIUM,
    "MEDIUM": Severity.MEDIUM,
    "LOW": Severity.LOW,
    "INFO": Severity.INFO,
    "EXPERIMENT": Severity.INFO,
    "INVENTORY": Severity.INFO,
}


def _stable_key(check_id: str, path: str, start_line: int, end_line: int) -> str:
    """
    Deterministic fallback finding key for when `extra.fingerprint` isn't
    present. Confirmed live (semgrep-interfaces' schema): fingerprint has
    required being logged in to semgrep.dev since Semgrep 1.98.0, so a
    plain anonymous `semgrep scan` never has one. This hash is stable
    across identical re-scans of unchanged code, but - unlike SonarQube's
    server-tracked issue keys - a finding that merely shifts line numbers
    (code inserted above it) hashes differently and looks "new",
    orphaning its old ticket. A known, documented limitation (see
    docs/ARCHITECTURE.md), not a bug.
    """
    raw = f"{check_id}|{path}|{start_line}|{end_line}"
    return f"semgrep:{hashlib.sha256(raw.encode()).hexdigest()[:16]}"


class SemgrepLocalClient(ScannerClient):
    def fetch_findings(self, project_key: str, branch: str | None = None) -> list[Finding]:
        """
        `project_key` is the path to scan, not a server-registered
        identifier - Semgrep OSS has no server, so "run the scan" and
        "fetch its results" are the same synchronous operation here.
        `branch` is accepted only to satisfy the ScannerClient contract;
        Semgrep has no concept of it - the caller (fetch_findings_activity)
        stamps Finding.branch afterward, same as every other scanner.
        """
        result = subprocess.run(
            ["semgrep", "scan", "--json", "--config", "auto", "--quiet", project_key],
            capture_output=True,
            text=True,
            check=False,
        )
        # semgrep exits 1 when it finds results - not an error - and only
        # 2+ on a genuine failure (bad config, crash). stdout is still
        # valid JSON in the exit-1 case.
        if result.returncode >= 2:
            raise RuntimeError(f"semgrep scan exited with status {result.returncode}: {result.stderr}")

        data = json.loads(result.stdout or "{}")
        return [self._build_finding(raw) for raw in data.get("results", [])]

    def _build_finding(self, raw: dict) -> Finding:
        check_id = raw.get("check_id", "")
        path = raw.get("path", "")
        start = raw.get("start", {})
        end = raw.get("end", {})
        start_line = start.get("line")
        extra = raw.get("extra", {})

        fingerprint = extra.get("fingerprint")
        key = (
            f"semgrep:{fingerprint}"
            if fingerprint
            else _stable_key(check_id, path, start_line or 0, end.get("line", start_line) or 0)
        )
        location = f"{path}:{start_line}" if start_line is not None else path

        return Finding(
            key=key,
            title=f"{check_id} ({location})",
            severity=self._map_severity(extra.get("severity")),
            component=path,
            line=start_line,
            message=extra.get("message", ""),
            finding_type="vulnerability",  # Semgrep has no hotspot-style category
            deep_link=f"https://semgrep.dev/r/{check_id}" if check_id else "https://semgrep.dev",
            source_tool="semgrep",
            rule_key=check_id or None,
        )

    def _map_severity(self, raw_severity: str | None) -> Severity:
        # No activity.logger here (unlike scanner/sonarqube_common.py's
        # equivalent) - this runs host-side in the CLI process, never
        # inside a Temporal activity, so that logger has no context to
        # attach to. Every real Semgrep severity value is covered above;
        # falling back silently only matters for a genuinely future/unknown
        # value.
        return SEMGREP_SEVERITY_MAP.get(raw_severity, DEFAULT_SEVERITY)

    def requirements(self) -> ScannerRequirements:
        return ScannerRequirements(docker_services=[], host_dependencies=["semgrep"])

    def fetch_resolutions(self, finding_keys: list[str]) -> dict[str, str]:
        """
        Always empty: reconciliation
        (temporal/activities/reconcile_resolved_findings.py) runs inside
        the Temporal worker, which has no access to the host checkout this
        scan ran against - there's no way to re-scan and check whether a
        finding is now fixed from there. A local Semgrep finding's ticket
        is only ever closed manually. Documented in
        docs/ARCHITECTURE.md's "Known limitations".
        """
        return {}
