# Copyright (c) 2026 Calfus Inc.
# Author: Wasiullah Rafeeq S

"""
Small builders for Atlassian Document Format (ADF) nodes - Jira's rich-text
JSON format for issue descriptions/comments. Kept generic (no Finding
knowledge) so jira_client.py can compose them instead of hand-writing
nested dicts per field.
"""


def doc(*content: dict) -> dict:
    return {"type": "doc", "version": 1, "content": list(content)}


def paragraph(text: str, link: str | None = None) -> dict:
    node: dict = {"type": "text", "text": text}
    if link:
        node["marks"] = [{"type": "link", "attrs": {"href": link}}]
    return {"type": "paragraph", "content": [node]}


def bullet_list(lines: list[str]) -> dict:
    return {
        "type": "bulletList",
        "content": [{"type": "listItem", "content": [paragraph(line)]} for line in lines],
    }


def code_block(text: str) -> dict:
    return {"type": "codeBlock", "attrs": {}, "content": [{"type": "text", "text": text}]}


def heading(text: str, level: int = 3) -> dict:
    return {"type": "heading", "attrs": {"level": level}, "content": [{"type": "text", "text": text}]}


def expand(title: str, *content: dict) -> dict:
    """Collapsible section - keeps a long body out of the way until clicked."""
    return {"type": "expand", "attrs": {"title": title}, "content": list(content)}


def summarize(text: str, limit: int = 400) -> tuple[str, str | None]:
    """(short, full) - `full` is None when `text` already fits in `limit`. Whitespace is collapsed; the cut prefers a sentence end, then a word boundary."""
    clean = " ".join(text.split())
    if len(clean) <= limit:
        return clean, None
    window = clean[:limit]
    cut = max(window.rfind(". "), window.rfind("! "), window.rfind("? "))
    if cut < limit // 2:
        cut = window.rfind(" ")
    if cut < limit // 2:
        return window.rstrip() + "...", clean
    return window[: cut + 1].rstrip() + (" ..." if window[cut] != "." else ""), clean
