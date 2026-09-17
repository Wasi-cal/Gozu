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
from pathlib import Path

from core.models import Finding, Severity
from scanner.base import DEFAULT_SEVERITY, ScannerClient, ScannerRequirements

# Lines of context to read on each side of the flagged range, straight off
# the host checkout - generous enough to serve both llm/enrich.py's prompt
# (which wants more context than a rendered image strictly needs) and
# scanner/screenshot.py's PNG render off the SAME captured window, rather
# than reading the file twice with two different widths.
_CONTEXT_LINES = 15

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


def _read_context_snippet(root: str, relative_path: str, start_line: int, end_line: int) -> tuple[str | None, int | None]:
    """
    Reads ±_CONTEXT_LINES of plain-text source around the flagged range
    directly off the host checkout - the one moment this scanner has
    filesystem access to it at all (see this module's own docstring). A
    read failure (file since renamed/deleted between the match and this
    read, permission issue, ...) returns (None, None) rather than raising -
    a missing snippet must never fail the scan itself, same "best-effort,
    never load-bearing" treatment SonarQube's own snippet fetch gets.
    """
    try:
        lines = (Path(root) / relative_path).read_text(errors="replace").splitlines()
    except OSError:
        return None, None

    if not lines:
        return None, None

    from_line = max(1, start_line - _CONTEXT_LINES)
    to_line = min(len(lines), end_line + _CONTEXT_LINES)
    if from_line > len(lines):
        return None, None

    return "\n".join(lines[from_line - 1 : to_line]), from_line


class SemgrepLocalClient(ScannerClient):
    def fetch_findings(self, project_key: str, branch: str | None = None) -> list[Finding]:
        """
        `project_key` is the path to scan, not a server-registered
        identifier - Semgrep OSS has no server, so "run the scan" and
        "fetch its results" are the same synchronous operation here.
        `branch` is accepted only to satisfy the ScannerClient contract;
        Semgrep has no concept of it - the caller (fetch_findings_activity)
        stamps Finding.branch afterward, same as every other scanner.

        Invoked with `cwd=project_key` and target "." (rather than passing
        `project_key` as the scan target directly) so every result's own
        `path` is reliably relative to `project_key` - needed to read the
        flagged file back for its snippet (_read_context_snippet()) without
        guessing how semgrep resolved a possibly-relative target itself.
        """
        result = subprocess.run(
            ["semgrep", "scan", "--json", "--config", "auto", "--quiet", "."],
            cwd=project_key,
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
        return [self._build_finding(raw, project_key) for raw in data.get("results", [])]

    def _build_finding(self, raw: dict, root: str) -> Finding:
        check_id = raw.get("check_id", "")
        path = raw.get("path", "")
        start = raw.get("start", {})
        end = raw.get("end", {})
        start_line = start.get("line")
        end_line = end.get("line", start_line)
        extra = raw.get("extra", {})

        fingerprint = extra.get("fingerprint")
        key = (
            f"semgrep:{fingerprint}"
            if fingerprint
            else _stable_key(check_id, path, start_line or 0, end_line or 0)
        )
        location = f"{path}:{start_line}" if start_line is not None else path

        code_snippet, code_snippet_start_line = (
            _read_context_snippet(root, path, start_line, end_line or start_line)
            if start_line is not None
            else (None, None)
        )

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
            code_snippet=code_snippet,
            code_snippet_start_line=code_snippet_start_line,
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
