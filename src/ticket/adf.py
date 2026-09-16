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
