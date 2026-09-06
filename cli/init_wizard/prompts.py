# Copyright (c) 2026 Calfus Inc.
# Author: Wasiullah Rafeeq S

"""
Shared questionary-prompt helpers for the init wizard. Every select/confirm
prompt uses questionary (arrow-key menus), not typed option strings - a
deliberate, fixed choice given how many branching selects this wizard has.
"""

import secrets

import questionary
import typer

from cli.help_links import print_help_link
from cli.status import warning


def ask_or_exit(question: questionary.Question) -> str:
    """questionary returns None on Ctrl-C/Ctrl-D - treat that as a clean abort, not a crash."""
    answer = question.ask()
    if answer is None:
        raise typer.Exit(code=1)
    return answer


def prompt_text(label: str, field: str | None = None, default: str = "") -> str:
    if field:
        print_help_link(field)
    return ask_or_exit(questionary.text(label, default=default))


def generate_or_prompt_secret(label: str) -> str:
    secret = ask_or_exit(questionary.text(f"{label} (leave blank to generate one):"))
    if secret:
        return secret
    generated = secrets.token_urlsafe(32)
    warning(f"Generated: {generated}")
    typer.secho("(shown once - it's stored encrypted, this is your only chance to copy it elsewhere)", dim=True)
    return generated
