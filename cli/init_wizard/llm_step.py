# Copyright (c) 2026 Calfus Inc.
# Author: Prakrit Mohanty

"""
Optional LLM-enrichment credential collection for the init wizard and
`gozu config edit` - unlike SonarQube/Jira, this credential isn't required
for gozu to work at all: leaving it blank means Blocker/Critical/High
tickets keep today's templated description instead of an LLM-generated
explanation (see llm/enrich.py, temporal/activities/create_tickets.py).
"""

from cli.prompts import prompt_text


def prompt_anthropic_api_key(default: str = "") -> str:
    return prompt_text(
        "Anthropic API key (optional - blank disables AI-generated ticket explanations):",
        field="anthropic_api_key",
        default=default,
    )
