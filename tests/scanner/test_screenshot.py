# Copyright (c) 2026 Calfus Inc.
# Author: Wasiullah Rafeeq S

from unittest.mock import MagicMock, patch

import requests

from core.models import Finding, Severity
from scanner.screenshot import fetch_snippet_lines, _host_url_from_deep_link, render_finding_snippet


def make_finding(line: int | None = 5, component: str = "proj:hello.py", deep_link: str | None = None) -> Finding:
    return Finding(
        key="proj:hello.py:1",
        title="Weak hash algorithm",
        severity=Severity.HIGH,
        component=component,
        line=line,
        message="Make sure that hashing data is safe here.",
        finding_type="vulnerability",
        deep_link=deep_link or "http://localhost:9000/project/issues?id=proj&issues=x",
        source_tool="sonarqube",
    )


def make_sources_response(lines: dict[int, str]) -> MagicMock:
    response = MagicMock()
    response.status_code = 200
    response.raise_for_status = MagicMock()
    response.json.return_value = {"sources": [{"line": line, "code": code} for line, code in lines.items()]}
    return response


# --- Host resolution: reuse the finding's own deep_link, no separate plumbing ---


def test_host_url_from_deep_link_extracts_scheme_and_netloc():
    assert _host_url_from_deep_link("https://sonar.example.com:9000/project/issues?id=proj") == "https://sonar.example.com:9000"


# --- Fetching + clamping ---


def test_fetch_snippet_lines_clamps_from_line_to_one():
    finding = make_finding(line=2)
    response = make_sources_response({1: "a", 2: "b", 3: "c", 4: "d", 5: "e", 6: "f", 7: "g"})
    with patch("scanner.screenshot.requests.get", return_value=response) as mock_get:
        with patch("scanner.screenshot.resolve_container_host", side_effect=lambda url: url):
            lines, from_line = fetch_snippet_lines(finding, "tok", context_lines=5)

    assert from_line == 1  # line=2, context=5 -> raw from would be -3, clamped to 1
    called_params = mock_get.call_args.kwargs["params"]
    assert called_params["from"] == 1
    assert called_params["to"] == 7  # line + context_lines, uncapped - SonarQube itself clamps past EOF


def test_fetch_snippet_lines_strips_html_from_each_line():
    finding = make_finding(line=1)
    response = make_sources_response({1: '<span class="k">import</span> hashlib'})
    with patch("scanner.screenshot.requests.get", return_value=response):
        with patch("scanner.screenshot.resolve_container_host", side_effect=lambda url: url):
            lines, _ = fetch_snippet_lines(finding, "tok", context_lines=0)

    assert lines == ["import hashlib"]


def test_fetch_snippet_lines_raises_on_error_response():
    finding = make_finding()
    response = MagicMock()
    response.raise_for_status.side_effect = requests.HTTPError("400")
    with patch("scanner.screenshot.requests.get", return_value=response):
        with patch("scanner.screenshot.resolve_container_host", side_effect=lambda url: url):
            try:
                fetch_snippet_lines(finding, "tok", context_lines=5)
                assert False, "expected HTTPError to propagate"
            except requests.HTTPError:
                pass


# --- The actual point: get the offset right, confirmed live against real
# SonarQube data with a non-trivial from_line (see session notes) ---


def test_render_finding_snippet_highlights_correct_line_when_context_starts_mid_file():
    """finding.line=13, context_lines=5 -> from_line=8 -> highlighted line is index 6 within the snippet, not 13."""
    finding = make_finding(line=13)
    lines = {n: f"line {n}" for n in range(8, 19)}  # from=8 to=18
    response = make_sources_response(lines)

    with patch("scanner.screenshot.requests.get", return_value=response):
        with patch("scanner.screenshot.resolve_container_host", side_effect=lambda url: url):
            with patch("scanner.screenshot.ImageFormatter") as mock_formatter_cls:
                mock_formatter = MagicMock()
                mock_formatter_cls.return_value = mock_formatter
                mock_formatter.format = MagicMock()
                render_finding_snippet(finding, "tok")

    _, kwargs = mock_formatter_cls.call_args
    assert kwargs["hl_lines"] == [6]  # 13 - 8 + 1
    assert kwargs["line_number_start"] == 8


def test_render_finding_snippet_highlights_line_one_when_finding_at_file_start():
    finding = make_finding(line=1)
    lines = {n: f"line {n}" for n in range(1, 7)}  # from=1 (clamped) to=6
    response = make_sources_response(lines)

    with patch("scanner.screenshot.requests.get", return_value=response):
        with patch("scanner.screenshot.resolve_container_host", side_effect=lambda url: url):
            with patch("scanner.screenshot.ImageFormatter") as mock_formatter_cls:
                mock_formatter = MagicMock()
                mock_formatter_cls.return_value = mock_formatter
                render_finding_snippet(finding, "tok")

    _, kwargs = mock_formatter_cls.call_args
    assert kwargs["hl_lines"] == [1]


def test_render_finding_snippet_returns_real_png_bytes():
    """No mocked formatter here - a real end-to-end render, checked for an actual PNG signature."""
    finding = make_finding(line=2, component="proj:hello.py")
    response = make_sources_response({1: "import hashlib", 2: "print(hashlib)", 3: ""})

    with patch("scanner.screenshot.requests.get", return_value=response):
        with patch("scanner.screenshot.resolve_container_host", side_effect=lambda url: url):
            png_bytes = render_finding_snippet(finding, "tok", context_lines=1)

    assert png_bytes.startswith(b"\x89PNG\r\n\x1a\n")
    assert len(png_bytes) > 100


def test_render_finding_snippet_picks_lexer_from_extension():
    finding = make_finding(line=1, component="proj:src/app.ts")
    response = make_sources_response({1: "const x = 1;"})

    with patch("scanner.screenshot.requests.get", return_value=response):
        with patch("scanner.screenshot.resolve_container_host", side_effect=lambda url: url):
            with patch("scanner.screenshot.lexers.get_lexer_for_filename") as mock_get_lexer:
                from pygments.lexers import TypeScriptLexer

                mock_get_lexer.return_value = TypeScriptLexer()
                render_finding_snippet(finding, "tok", context_lines=0)

    mock_get_lexer.assert_called_once()
    assert mock_get_lexer.call_args.args[0] == "src/app.ts"


def test_render_finding_snippet_falls_back_to_text_lexer_for_unknown_extension():
    from pygments.util import ClassNotFound

    finding = make_finding(line=1, component="proj:Dockerfile.weird-ext")
    response = make_sources_response({1: "FROM python:3.12"})

    with patch("scanner.screenshot.requests.get", return_value=response):
        with patch("scanner.screenshot.resolve_container_host", side_effect=lambda url: url):
            with patch("scanner.screenshot.lexers.get_lexer_for_filename", side_effect=ClassNotFound("no match")):
                # Must not raise - falls back to TextLexer instead of propagating ClassNotFound.
                png_bytes = render_finding_snippet(finding, "tok", context_lines=0)

    assert png_bytes.startswith(b"\x89PNG\r\n\x1a\n")
