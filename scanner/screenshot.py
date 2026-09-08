# Copyright (c) 2026 Calfus Inc.
# Author: Wasiullah Rafeeq S
# Editor: Prakrit Mohanty
#
# Depends on: scanner/base.py - resolve_container_host() for Docker container-host URL rewriting
# Depends on: scanner/html_text.py - strip_html() for SonarQube's syntax-highlighting markup

"""
Renders a syntax-highlighted PNG snippet of the source lines around a
finding, server-side via Pygments

render_finding_snippet() fetches the raw source lines directly from
SonarQube's REST API (/api/sources/lines), not by rendering any page -
this process was already able to make that same authenticated call for
everything else (scanner/sonarqube_common.py), so this needed no new
capability, just a new endpoint.

The annotation text SonarQube's own UI showed alongside the snippet
(the old Playwright code scraped it from the rendered DOM) is simply
`finding.message` now - the exact same string the API already gave us
when the finding was first fetched, never scraped from anywhere.
"""

import io
from urllib.parse import urlsplit

import requests
from pygments import lexers
from pygments.formatters import ImageFormatter
from pygments.util import ClassNotFound

from core.models import Finding
from scanner.base import resolve_container_host
from scanner.html_text import strip_html

DEFAULT_CONTEXT_LINES = 5


def _host_url_from_deep_link(deep_link: str) -> str:
    """
    The finding's own deep_link already encodes exactly which SonarQube
    host it came from (scanner/sonarqube_common.py builds it as
    "{base_url}/project/issues?id=..."), for both Local and Cloud
    configs - reusing it here means render_finding_snippet() needs no
    separate host_url/scanner_mode plumbing threaded through
    ScreenshotAttachInput just to find the same information a second way.
    """
    parts = urlsplit(deep_link)
    return f"{parts.scheme}://{parts.netloc}"


def fetch_snippet_lines(finding: Finding, token: str, context_lines: int) -> tuple[list[str], int]:
    """
    Returns (plain-text source lines, the first line's real file line
    number) - the caller needs that offset to know where the flagged
    line falls WITHIN the returned snippet, not just within the file.

    Public - llm/enrich.py is a second caller (fetches a wider context
    window to hand to the LLM alongside the rule's how-to-fix guidance).
    """
    line = finding.line or 1
    from_line = max(1, line - context_lines)
    to_line = line + context_lines  # SonarQube itself clamps a `to` past EOF - confirmed live, no error.

    request_base_url = resolve_container_host(_host_url_from_deep_link(finding.deep_link))
    response = requests.get(
        f"{request_base_url}/api/sources/lines",
        params={"key": finding.component, "from": from_line, "to": to_line},
        auth=(token, ""),
    )
    response.raise_for_status()

    sources = response.json().get("sources", [])
    lines = [strip_html(source.get("code", "")) for source in sources]
    return lines, from_line


def render_finding_snippet(finding: Finding, token: str, context_lines: int = DEFAULT_CONTEXT_LINES) -> bytes:
    """
    Fetch the source lines around `finding.line` (±`context_lines`) and
    render them as a syntax-highlighted PNG, the flagged line
    highlighted. Raises on any failure (a bad response, no lexer
    somehow, ...) rather than swallowing it - the caller
    (temporal/activities/capture_and_attach_screenshot.py) is what
    decides how to make that visible, not this function.
    """
    lines, from_line = fetch_snippet_lines(finding, token, context_lines)
    code = "\n".join(lines)

    path = finding.component.split(":", 1)[-1]  # component is "{project_key}:{relative/path}"
    try:
        lexer = lexers.get_lexer_for_filename(path, code)
    except ClassNotFound:
        lexer = lexers.TextLexer()

    # The flagged line's position WITHIN the snippet (1-indexed, matching
    # Pygments' hl_lines convention), not its absolute file line number -
    # e.g. finding.line=42, from_line=37 (context_lines=5) -> line 6 of
    # the 11-line snippet, not 42.
    highlighted_line = (finding.line or from_line) - from_line + 1

    formatter = ImageFormatter(
        # pygments-stubs' Formatter.__init__ overloads only bind
        # Formatter[bytes] (what ImageFormatter always is) when either
        # `encoding` or `outencoding` is explicitly a `str` - passing
        # neither, even though ImageFormatter itself doesn't actually use
        # `encoding` when rendering, is what pyright's "no overloads
        # match" was about. Any str value satisfies the overload.
        encoding="utf-8",
        line_numbers=True,
        line_number_start=from_line,
        hl_lines=[highlighted_line],
        font_size=16,
        line_pad=4,
    )
    buffer = io.BytesIO()
    formatter.format(lexer.get_tokens(code), buffer)
    return buffer.getvalue()
