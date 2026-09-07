# Copyright (c) 2026 Calfus Inc.
# Author: Wasiullah Rafeeq S

"""
Shared questionary-prompt helpers - used by the init wizard
(cli/init_wizard/*), the shared review/edit engine (cli/wizard_engine.py),
and gozu config edit (cli/config_cmd/). Every select/confirm prompt uses
questionary (arrow-key menus), not typed option strings - a deliberate,
fixed choice given how many branching selects these flows have.

Deliberately NOT nested under cli/init_wizard/ (it lived there originally)
- cli/wizard_engine.py needs ask_or_exit() too, and importing a submodule
of cli.init_wizard would import cli/init_wizard/__init__.py first, which
itself needs to import cli.wizard_engine - a real circular import, not a
hypothetical one. Living at the top level of cli/ instead means anything
under cli/ can depend on these primitives without that risk.
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
