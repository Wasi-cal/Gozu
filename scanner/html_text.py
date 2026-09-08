# Copyright (c) 2026 Calfus Inc.
# Author: Wasiullah Rafeeq S

"""
Shared HTML-to-plain-text stripping - SonarQube marks up more than one kind
of text this way: /api/sources/lines' `code` field (scanner/screenshot.py,
its own syntax-highlighting spans) and a rule's /api/rules/show description
sections (scanner/sonarqube_common.py, prose + code examples). Split out
once the same need showed up in both places rather than duplicating the
HTMLParser subclass.
"""

from html.parser import HTMLParser

# Tags SonarQube's rule-description HTML actually uses that should read as
# a line break in plain text - a rule's HTML has real paragraph/heading/
# list/code structure (unlike sources/lines' inline-only spans), so
# stripping tags with no break at all would run everything together.
_BLOCK_TAGS = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "pre", "br", "div"}


class _HTMLTextExtractor(HTMLParser):
    def __init__(self, preserve_block_breaks: bool) -> None:
        super().__init__()
        self._parts: list[str] = []
        self._preserve_block_breaks = preserve_block_breaks

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._preserve_block_breaks and tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        self._parts.append(data)

    def text(self) -> str:
        return "".join(self._parts)


def strip_html(marked_up: str, preserve_block_breaks: bool = False) -> str:
    """
    Strips every tag, keeping only text content - HTMLParser's default
    convert_charrefs=True already decodes entities (&amp; -> &, etc) in
    the text handed to handle_data(), so no separate unescape step is
    needed.

    `preserve_block_breaks=True` (rule descriptions) turns block-level
    tags into line breaks first, then collapses the runs of blank
    lines/whitespace that produces into at most one blank line each -
    readable plain text, not a 1:1 HTML reproduction. Default False
    (sources/lines' inline-only spans) matches plain concatenation, the
    same behavior this had before rule descriptions needed the other mode.
    """
    extractor = _HTMLTextExtractor(preserve_block_breaks)
    extractor.feed(marked_up)
    text = extractor.text()
    if not preserve_block_breaks:
        return text

    collapsed: list[str] = []
    for line in (line.strip() for line in text.splitlines()):
        if line or (collapsed and collapsed[-1]):
            collapsed.append(line)
    return "\n".join(collapsed).strip()
