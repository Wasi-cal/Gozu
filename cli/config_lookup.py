# Copyright (c) 2026 Calfus Inc.
# Author: Wasiullah Rafeeq S
# Editor: Claude
#
# Depends on: config/store.py - looking up and listing saved configs

"""
Shared "a --config <name> was given but doesn't match anything" recovery,
used everywhere a config is looked up by name that the caller typed
explicitly (gozu run --config, gozu up --config, gozu config edit/delete)
- as opposed to cli/scan_runner/config_select.py's select_config(), which
this module is a sibling to, not a replacement for: select_config() also
handles the *no name given at all* picker (auto-select the only one,
or choose among several), which is a different situation from a name
that was given but wrong. Kept here instead of folded into
config_select.py so cli/config_cmd and cli/stack don't have to import a
module named for scan_runner just to reuse this.

Never falls through to any default behavior on a miss - either the user
picks a real config from what actually exists, or explicitly exits.
"""

import questionary
import typer

import config.store as config_store
from cli.status import error


def resolve_config_or_prompt(name: str) -> dict:
    """
    Looks up a config by `name`. If found, returns it unchanged (the
    normal, correct-name path). If not:

    - Explains why the lookup failed ("No config named '<name>' found.")
    - If no configs exist at all, says so plainly and points at
      `gozu init` - there is nothing to pick from, so no picker is shown
    - Otherwise shows a questionary.select() of every existing config
      (name + scanner_mode + trigger_mode, the same informative format
      select_config() uses for its own no-name-given picker), plus a
      final "Exit" choice

    Picking a real config returns it - same shape as if that name had
    been passed correctly to begin with, so the caller proceeds exactly
    as normal. Picking "Exit" (or Ctrl-C, which questionary also surfaces
    as None) exits cleanly via typer.Exit - no traceback, no partial
    side effects, and never a silent fall-through to some default config.
    """
    config = config_store.get_config(name)
    if config is not None:
        return config

    error(f"No config named '{name}' found.")

    configs = config_store.list_configs()
    if not configs:
        typer.echo("No configs exist yet - run `gozu init` to create one.")
        raise typer.Exit(code=1)

    # A distinct sentinel, not None - questionary.Choice() defaults an
    # unset `value` to its own `title`, so a bare `value=None` here would
    # actually come back as the string "__exit__"... no, worse: it would
    # come back as the literal title "Exit", indistinguishable from a
    # real (if oddly named) config. Confirmed live: an earlier version of
    # this using value=None surfaced "Config 'Exit' disappeared before it
    # could be loaded." instead of exiting cleanly.
    _EXIT = "__exit__"
    answer = questionary.select(
        "Did you mean one of these instead?",
        choices=[
            questionary.Choice(
                title=f"{c['name']}  ({c['scanner_mode']}, trigger={c['trigger_mode']})",
                value=c["name"],
            )
            for c in configs
        ]
        + [questionary.Choice(title="Exit", value=_EXIT)],
    ).ask()
    if answer is None or answer == _EXIT:
        raise typer.Exit(code=1)

    resolved = config_store.get_config(answer)
    if resolved is None:
        # Vanishingly unlikely (deleted between list_configs() and here), but
        # get_config() can genuinely return None, so this has to be handled.
        error(f"Config '{answer}' disappeared before it could be loaded.")
        raise typer.Exit(code=1)
    return resolved
