# Copyright (c) 2026 Calfus Inc.
# Author: Prakrit Mohanty

import json
from unittest.mock import MagicMock, patch

from core.models import Severity
from scanner.semgrep_local import SemgrepLocalClient, _read_context_snippet, _stable_key

MODULE = "scanner.semgrep_local"


def make_result(
    check_id: str = "python.lang.security.audit.eval-detected.eval-detected",
    path: str = "app.py",
    start_line: int = 10,
    end_line: int = 10,
    message: str = "Detected eval used with user input.",
    severity: str = "ERROR",
    fingerprint: str | None = None,
) -> dict:
    return {
        "check_id": check_id,
        "path": path,
        "start": {"line": start_line, "col": 1},
        "end": {"line": end_line, "col": 5},
        "extra": {
            "message": message,
            "severity": severity,
            **({"fingerprint": fingerprint} if fingerprint else {}),
        },
    }


def mock_subprocess_run(results: list[dict], returncode: int = 1, stderr: str = ""):
    completed = MagicMock()
    completed.returncode = returncode
    completed.stdout = json.dumps({"results": results})
    completed.stderr = stderr
    return completed


def test_fetch_findings_maps_result_fields_into_finding():
    result = make_result()
    with patch(f"{MODULE}.subprocess.run", return_value=mock_subprocess_run([result])):
        findings = SemgrepLocalClient().fetch_findings("/some/path", branch="main")

    assert len(findings) == 1
    finding = findings[0]
    assert finding.component == "app.py"
    assert finding.line == 10
    assert finding.message == "Detected eval used with user input."
    assert finding.source_tool == "semgrep"
    assert finding.finding_type == "vulnerability"
    assert finding.rule_key == "python.lang.security.audit.eval-detected.eval-detected"
    assert finding.severity == Severity.HIGH  # ERROR -> HIGH


def test_fetch_findings_exit_code_1_is_not_an_error():
    """semgrep exits 1 when it finds results - confirmed behavior, not a failure."""
    with patch(f"{MODULE}.subprocess.run", return_value=mock_subprocess_run([make_result()], returncode=1)):
        findings = SemgrepLocalClient().fetch_findings("/some/path")
    assert len(findings) == 1


def test_fetch_findings_exit_code_2_plus_raises():
    with patch(
        f"{MODULE}.subprocess.run",
        return_value=mock_subprocess_run([], returncode=2, stderr="invalid pattern"),
    ):
        try:
            SemgrepLocalClient().fetch_findings("/some/path")
            assert False, "expected RuntimeError"
        except RuntimeError as e:
            assert "invalid pattern" in str(e)


def test_fetch_findings_no_results_returns_empty_list():
    with patch(f"{MODULE}.subprocess.run", return_value=mock_subprocess_run([], returncode=0)):
        findings = SemgrepLocalClient().fetch_findings("/some/path")
    assert findings == []


# --- Severity mapping ---


def test_severity_map_covers_every_documented_semgrep_value():
    for raw, expected in [
        ("CRITICAL", Severity.CRITICAL),
        ("ERROR", Severity.HIGH),
        ("HIGH", Severity.HIGH),
        ("WARNING", Severity.MEDIUM),
        ("MEDIUM", Severity.MEDIUM),
        ("LOW", Severity.LOW),
        ("INFO", Severity.INFO),
        ("EXPERIMENT", Severity.INFO),
        ("INVENTORY", Severity.INFO),
    ]:
        result = make_result(severity=raw)
        with patch(f"{MODULE}.subprocess.run", return_value=mock_subprocess_run([result])):
            findings = SemgrepLocalClient().fetch_findings("/some/path")
        assert findings[0].severity == expected, f"{raw} should map to {expected}"


def test_unrecognized_severity_falls_back_to_default_without_raising():
    result = make_result(severity="SOME_FUTURE_VALUE")
    with patch(f"{MODULE}.subprocess.run", return_value=mock_subprocess_run([result])):
        findings = SemgrepLocalClient().fetch_findings("/some/path")
    assert findings[0].severity == Severity.MEDIUM  # DEFAULT_SEVERITY


# --- Dedup key: fingerprint when present, deterministic hash fallback otherwise ---


def test_key_uses_fingerprint_when_present():
    result = make_result(fingerprint="abc123")
    with patch(f"{MODULE}.subprocess.run", return_value=mock_subprocess_run([result])):
        findings = SemgrepLocalClient().fetch_findings("/some/path")
    assert findings[0].key == "semgrep:abc123"


def test_key_falls_back_to_stable_hash_without_fingerprint():
    result = make_result(fingerprint=None)
    with patch(f"{MODULE}.subprocess.run", return_value=mock_subprocess_run([result])):
        findings = SemgrepLocalClient().fetch_findings("/some/path")
    assert findings[0].key == _stable_key("python.lang.security.audit.eval-detected.eval-detected", "app.py", 10, 10)


def test_stable_key_is_deterministic_for_identical_input():
    assert _stable_key("rule-x", "a.py", 5, 6) == _stable_key("rule-x", "a.py", 5, 6)


def test_stable_key_differs_when_line_shifts():
    """Documented limitation: a finding that shifts lines looks 'new' without a fingerprint."""
    assert _stable_key("rule-x", "a.py", 5, 6) != _stable_key("rule-x", "a.py", 6, 7)


# --- requirements() / fetch_resolutions() ---


def test_requirements_needs_only_the_semgrep_binary():
    requirements = SemgrepLocalClient().requirements()
    assert requirements.docker_services == []
    assert requirements.host_dependencies == ["semgrep"]


def test_fetch_resolutions_always_returns_empty_dict():
    """No auto-close for local Semgrep - reconciliation runs in the worker, which can't re-scan the host checkout."""
    assert SemgrepLocalClient().fetch_resolutions(["semgrep:abc", "semgrep:def"]) == {}


# --- Code snippet capture: read host-side, since this is the one moment this scanner has filesystem access ---


def test_fetch_findings_reads_code_snippet_from_the_real_scanned_file(tmp_path):
    source = "\n".join(f"line {i}" for i in range(1, 31))  # 30 lines
    (tmp_path / "app.py").write_text(source)
    result = make_result(path="app.py", start_line=20, end_line=20)

    with patch(f"{MODULE}.subprocess.run", return_value=mock_subprocess_run([result])):
        findings = SemgrepLocalClient().fetch_findings(str(tmp_path))

    finding = findings[0]
    assert finding.code_snippet_start_line == 5  # 20 - 15 context lines
    assert finding.code_snippet.splitlines()[0] == "line 5"
    assert finding.code_snippet.splitlines()[-1] == "line 30"  # clamped to EOF (20 + 15 = 35 > 30 lines)


def test_fetch_findings_leaves_code_snippet_none_when_file_is_missing(tmp_path):
    result = make_result(path="does-not-exist.py")
    with patch(f"{MODULE}.subprocess.run", return_value=mock_subprocess_run([result])):
        findings = SemgrepLocalClient().fetch_findings(str(tmp_path))

    assert findings[0].code_snippet is None
    assert findings[0].code_snippet_start_line is None


def test_read_context_snippet_clamps_to_line_1_near_the_top_of_the_file(tmp_path):
    (tmp_path / "a.py").write_text("\n".join(f"line {i}" for i in range(1, 6)))  # 5 lines
    snippet, from_line = _read_context_snippet(str(tmp_path), "a.py", start_line=2, end_line=2)
    assert from_line == 1
    assert snippet.splitlines() == ["line 1", "line 2", "line 3", "line 4", "line 5"]


def test_read_context_snippet_clamps_to_eof(tmp_path):
    (tmp_path / "a.py").write_text("\n".join(f"line {i}" for i in range(1, 6)))  # 5 lines
    snippet, from_line = _read_context_snippet(str(tmp_path), "a.py", start_line=5, end_line=5)
    assert from_line == 1  # 5 - 15 context lines, clamped
    assert snippet.splitlines()[-1] == "line 5"


def test_read_context_snippet_returns_none_for_a_missing_file(tmp_path):
    assert _read_context_snippet(str(tmp_path), "missing.py", start_line=1, end_line=1) == (None, None)
