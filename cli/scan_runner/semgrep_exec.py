# Copyright (c) 2026 Calfus Inc.
# Author: Prakrit Mohanty
#
# Depends on: cli/prerequisites/semgrep.py - ensuring semgrep is installed before scanning
# Depends on: scanner/semgrep_local.py - the actual scan + JSON-parsing logic this reuses

"""Running a local Semgrep scan host-side and getting back normalized Findings."""

from cli.prerequisites.semgrep import ensure_semgrep
from core.models import Finding
from scanner.semgrep_local import SemgrepLocalClient


def run_semgrep_local_scan(path: str, branch: str | None) -> list[Finding]:
    """
    Unlike SonarQube's run_scanner()/wait_for_analysis() split
    (cli/scan_runner/scanner_exec.py), there's no separate async
    server-side step to wait for - `semgrep scan` computes findings
    synchronously, so this both runs the scan and returns its result in
    one call. Reuses SemgrepLocalClient.fetch_findings() rather than
    duplicating its subprocess/JSON-parsing logic here.
    """
    ensure_semgrep()
    return SemgrepLocalClient().fetch_findings(path, branch=branch)
