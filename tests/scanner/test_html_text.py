# Copyright (c) 2026 Calfus Inc.
# Author: Wasiullah Rafeeq S

from scanner.html_text import strip_html

# --- Default mode (scanner/screenshot.py): confirmed live that SonarQube's
# /api/sources/lines `code` field is marked up (e.g.
# `<span class="k">import</span>`), not plain text. ---


def test_strip_html_removes_tags():
    assert strip_html('<span class="k">import</span> <span class="sym-1 sym">hashlib</span>') == "import hashlib"


def test_strip_html_decodes_entities():
    assert strip_html("a &amp;&amp; b &lt; c") == "a && b < c"


def test_strip_html_empty_string():
    assert strip_html("") == ""


# --- preserve_block_breaks mode (scanner/sonarqube_common.py's rule
# descriptions): block tags become line breaks instead of running
# straight into the next tag's text. ---


def test_strip_html_preserve_block_breaks_separates_paragraphs():
    result = strip_html("<p>First.</p><p>Second.</p>", preserve_block_breaks=True)
    assert result == "First.\nSecond."


def test_strip_html_preserve_block_breaks_collapses_blank_runs():
    # A run of several blank lines (h4's own break + the literal "\n\n" of
    # raw whitespace between the tags + p's own break) collapses to at
    # most one blank line, not zero - a single blank line is a paragraph
    # separator, same as normal prose, not itself considered noise.
    result = strip_html("<h4>Title</h4>\n\n\n<p>Body.</p>", preserve_block_breaks=True)
    assert result == "Title\n\nBody."
