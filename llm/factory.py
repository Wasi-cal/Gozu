# Copyright (c) 2026 Calfus Inc.
# Author: Prakrit Mohanty
#
# Depends on: Anthropic - Claude API client construction

"""
Pure-builder for the optional LLM-enrichment client - mirrors
scanner/factory.py's build_scanner_client()/ticket/factory.py's
build_ticket_client() pattern: no env reads here, just construction from
an explicit key. The caller (temporal/activities/create_tickets.py,
github_action/main.py) decides where that key comes from and whether it's
present at all - absence means "skip enrichment", never a call here.
"""

import anthropic


def build_llm_client(api_key: str) -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=api_key)
