# Copyright (c) 2026 Calfus Inc.
# Author: Wasiullah Rafeeq S

"""
Shared review/edit engine used by both `gozu init` (cli/init_wizard/) and
`gozu config edit` (cli/config_cmd/) - one implementation, not two. Only
handles the VALUE-level fields of a config (project_key, branches, and
credentials) - the structural/branching questions that decide WHICH
fields exist at all (scanner_type, scanner_mode, sonar_plan/Free-vs-
Premium, ticket-destination choice) are resolved by each caller before
ever building a field list, and are never themselves revisable through
this engine - editing "SonarQube project key" is a value correction;
editing "Local vs Cloud" would mean an entirely different set of fields
exists, a fundamentally bigger operation this engine deliberately doesn't
attempt.

Each WizardField wraps a zero-argument callable that does the actual
prompting - always one of the EXISTING per-field prompt functions already
in cli/init_wizard/* (prompt_text(), prompt_project_key(),
generate_or_prompt_secret(), the small prompt_jira_*() helpers, ...),
never new prompt logic duplicated here. This module only owns the
walk-once-then-review LOOP, not what any individual question asks or how
it validates - that discipline is what makes one engine safely reusable
for both a from-scratch wizard and a "fix one existing value" edit.

A field's prompt() typically needs to show the CURRENT value as its
default (e.g. re-editing project_key should prefill what's there now, not
start blank) - callers build each closure over the SAME mutable state
dict they pass in as `initial_state` (e.g. `lambda: prompt_text(...,
default=state.get("project_key", ""))`), which only works because
run_wizard() mutates that exact object in place rather than copying it -
a closure capturing a caller-local `state` variable would otherwise never
see edits the engine itself makes to a separate internal copy.
"""

from dataclasses import dataclass
from typing import Callable

import questionary

from cli.prompts import ask_or_exit

_SAVE = "__save__"


@dataclass
class WizardField:
    key: str  # the state dict key this field reads/writes
    label: str  # human label shown on the review screen (e.g. "SonarQube project key")
    prompt: Callable[[], str]  # re-invoked (with no arguments) whenever this field is selected to edit
    secret: bool = False  # mask the review-screen value if True (see _display_value())


def _display_value(field: WizardField, value: object) -> str:
    """Full value normally; secrets show only their last 4 characters (e.g. "****a1b2") - never the real value on the review screen."""
    if value in (None, ""):
        return "(not set)"
    text = str(value)
    if not field.secret:
        return text
    return f"****{text[-4:]}" if len(text) > 4 else "****"


def run_wizard(fields: list[WizardField], initial_state: dict, walk_first: bool) -> dict:
    """
    `walk_first=True` (gozu init): every field's prompt() is invoked once,
    in order, before the review screen ever appears - today's linear
    wizard flow, just built as a list first instead of executed inline.
    `walk_first=False` (gozu config edit): skips straight to the review
    screen against `initial_state`'s already-known values - nothing is
    (re-)prompted unless the user explicitly selects it.

    Either way, selecting a field on the review screen re-invokes just
    that field's prompt() and returns to the review screen, looping until
    "Looks good - save" is chosen. Returns `initial_state` itself (see the
    module docstring for why this mutates in place rather than copying) -
    the caller commits it (create_config() for init, update_config_fields()/
    update_config_credential() for edit); this engine never touches
    Postgres itself.
    """
    state = initial_state

    if walk_first:
        for field in fields:
            state[field.key] = field.prompt()

    while True:
        choices = [
            questionary.Choice(title=f"{field.label}: {_display_value(field, state.get(field.key))}", value=field.key)
            for field in fields
        ]
        choices.append(questionary.Choice(title="Looks good - save", value=_SAVE))

        chosen = ask_or_exit(questionary.select("Review your answers (select one to edit, or save):", choices=choices))
        if chosen == _SAVE:
            return state

        field = next(f for f in fields if f.key == chosen)
        state[field.key] = field.prompt()
