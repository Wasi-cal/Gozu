# Copyright (c) 2026 Calfus Inc.
# Author: Prakrit Mohanty
#
# Depends on: Anthropic - generates the ticket explanation itself
# Depends on: src/scanner/screenshot.py - fetch_snippet_lines() for the code around a finding

"""
Turns a Blocker/Critical/High finding into a plain-English explanation +
suggested fix, from SonarQube's rule guidance plus the flagged source code.
The caller decides eligibility and catches failures - this module raises
straight through on any API error.

Credential-leak guard: a finding whose rule specifically flags hardcoded
secrets (SonarQube's per-language "S2068 - Credentials should not be
hard-coded" rule, its dedicated `secrets:*` detection engine, or a Semgrep
rule whose id names the same category - see _is_credential_rule()) means
the flagged line itself IS the secret - the exact thing this feature must
never forward to a third-party API. enrich_finding() withholds the code
snippet entirely for those rules; it still sends the rule's own
how_to_fix guidance and the finding's generic message, which describe the
problem class without containing anyone's actual credential.
"""

import anthropic
from anthropic.types import TextBlock

from core.models import Finding
from scanner.screenshot import fetch_snippet_lines

# Wider than screenshot.py's DEFAULT_CONTEXT_LINES (5) - a rendered image
# crops to fit; a plain-text prompt has no such limit, and the LLM benefits
# from more surrounding code than a screenshot does.
LLM_CONTEXT_LINES = 15

_MODEL = "claude-opus-5"
_MAX_TOKENS = 1024

# "secrets:" is SonarQube's dedicated secrets-detection engine (e.g.
# "secrets:S6290"); ":S2068" is the per-language "Credentials should not be
# hard-coded" rule ("python:S2068", "java:S2068", ...).
_CREDENTIAL_RULE_PREFIXES = ("secrets:",)
_CREDENTIAL_RULE_SUFFIXES = (":S2068",)

# Semgrep has no fixed prefix/suffix convention the way SonarQube's rule
# keys do - its Registry names hardcoded-credential rules descriptively
# instead (e.g. "generic.secrets.security.detected-generic-api-key...",
# "python.lang.security.audit.hardcoded-password-string",
# "javascript.lang.security.detect-hardcoded-secret"). A rule key never
# contains either substring for SonarQube's own terse "language:S1234"
# format, so this substring check only ever matches Semgrep findings in
# practice - safe to apply unconditionally rather than branching on
# finding.source_tool.
_CREDENTIAL_RULE_KEYWORDS = ("secret", "hardcoded")

_SYSTEM_PROMPT = (
    "You write short, plain-English explanations for Jira tickets created from "
    "SonarQube findings. Given a rule's own guidance and (when available) the "
    "flagged code, explain in 2-4 sentences what the problem is and a concrete "
    "suggested fix. Write for a developer who will read this in a ticket "
    "description, not for the person who already knows the rule - no headers, "
    "no markdown, plain prose only."
)


def _is_credential_rule(rule_key: str | None) -> bool:
    if not rule_key:
        return False
    if rule_key.startswith(_CREDENTIAL_RULE_PREFIXES) or rule_key.endswith(_CREDENTIAL_RULE_SUFFIXES):
        return True
    lowered = rule_key.lower()
    return any(keyword in lowered for keyword in _CREDENTIAL_RULE_KEYWORDS)


# Default "no snippet" note - deliberately vague (a fetch failure, e.g. a
# since-renamed file, is not a credential concern) so the LLM isn't told
# something untrue about why the snippet is missing.
_SNIPPET_UNAVAILABLE_NOTE = "Code snippet not available for this finding."

# The specific note used when a snippet is withheld ON PURPOSE for a
# credential rule - this one IS shown to the LLM to steer it away from
# asking for or guessing at the actual secret value.
_CREDENTIAL_SNIPPET_WITHHELD_NOTE = (
    "Code snippet withheld: this rule flags hardcoded credentials, so the "
    "exact flagged text is never sent to this API. Explain the general risk "
    "and remediation without referring to a specific secret value."
)


def _build_prompt(finding: Finding, code_snippet: str | None, snippet_unavailable_note: str) -> str:
    parts = [f"Rule: {finding.title}", f"SonarQube's message: {finding.message}"]
    if finding.how_to_fix:
        parts.append(f"SonarQube's rule-level remediation guidance:\n{finding.how_to_fix}")
    if code_snippet:
        parts.append(f"Flagged code (around line {finding.line}):\n{code_snippet}")
    else:
        parts.append(snippet_unavailable_note)
    return "\n\n".join(parts)


def generate_explanation(
    client: anthropic.Anthropic,
    finding: Finding,
    code_snippet: str | None,
    snippet_unavailable_note: str = _SNIPPET_UNAVAILABLE_NOTE,
) -> str:
    response = client.messages.create(
        model=_MODEL,
        max_tokens=_MAX_TOKENS,
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": _build_prompt(finding, code_snippet, snippet_unavailable_note)}],
    )
    # A plain text-generation call with no tools/thinking - response.content
    # is still typed as a union of every possible block kind, so narrow to
    # TextBlock explicitly rather than assuming content[0] is one.
    text_blocks = [block.text for block in response.content if isinstance(block, TextBlock)]
    return "".join(text_blocks)


def enrich_finding(client: anthropic.Anthropic, finding: Finding, sonar_token: str) -> str:
    """
    Gets the code snippet (unless withheld per _is_credential_rule()) and
    generates the explanation. A finding whose scanner already captured
    its own snippet host-side (finding.code_snippet - e.g. local Semgrep,
    which has no remote API this could fetch from later) uses that
    directly; only a SonarQube finding with none falls back to fetching
    one live. A snippet-fetch failure there (e.g. the source file was
    since deleted/renamed) falls back to no snippet rather than failing
    enrichment outright - the rule guidance + message alone are still useful.
    """
    if _is_credential_rule(finding.rule_key):
        return generate_explanation(client, finding, None, _CREDENTIAL_SNIPPET_WITHHELD_NOTE)

    if finding.code_snippet is not None:
        return generate_explanation(client, finding, finding.code_snippet)

    if finding.source_tool != "sonarqube":
        # No pre-captured snippet and no remote source API to fetch one
        # from - proceed without a snippet rather than calling SonarQube's
        # API against a finding that was never SonarQube's.
        return generate_explanation(client, finding, None)

    try:
        lines, _ = fetch_snippet_lines(finding, sonar_token, LLM_CONTEXT_LINES)
        code_snippet = "\n".join(lines) or None
    except Exception:
        code_snippet = None
    return generate_explanation(client, finding, code_snippet)
