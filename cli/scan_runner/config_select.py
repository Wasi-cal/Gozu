# Copyright (c) 2026 Calfus Inc.
# Author: Wasiullah Rafeeq S
# Editor: Prakrit Mohanty, Claude
#
# Depends on: config/store.py - listing/looking up saved configs
# Depends on: cli/config_lookup.py - resolving a config name typed by the user
# Depends on: cli/stack/__init__.py - ensuring the config store is ready before use

"""Resolving which saved config `gozu run` should use."""

import questionary
import typer

import config.store as config_store
from cli.config_lookup import resolve_config_or_prompt
from cli.stack import ensure_config_store_ready
from cli.status import error


def select_config(name: str | None) -> dict:
    """
    A given `name` is looked up via resolve_config_or_prompt() - a miss
    explains why, then offers a picker of what actually exists (plus
    Exit) rather than a bare error, same as gozu up/config edit/config
    delete. With no name: errors if none exist ("run `gozu init`
    first"), auto-selects (and announces) the only one if exactly one
    exists, or shows an informed questionary select (name + scanner_mode +
    trigger_mode, not just a bare name list) if there are several.
    """
    ensure_config_store_ready()

    if name:
        return resolve_config_or_prompt(name)

    configs = config_store.list_configs()
    if not configs:
        error("No configs found - run `gozu init` first.")
        raise typer.Exit(code=1)

    if len(configs) == 1:
        chosen_name = configs[0]["name"]
        typer.echo(f"Auto-selected the only config: {chosen_name}")
    else:
        answer = questionary.select(
            "Which config?",
            choices=[
                questionary.Choice(
                    title=f"{c['name']}  ({c['scanner_mode']}, trigger={c['trigger_mode']})",
                    value=c["name"],
                )
                for c in configs
            ],
        ).ask()
        if answer is None:
            raise typer.Exit(code=1)
        chosen_name = answer

    config = config_store.get_config(chosen_name)
    if config is None:
        # Vanishingly unlikely (deleted between list_configs() and here), but
        # get_config() can genuinely return None, so this has to be handled.
        error(f"Config '{chosen_name}' disappeared before it could be loaded.")
        raise typer.Exit(code=1)
    return config
