# Copyright (c) 2026 Calfus Inc.
# Author: Wasiullah Rafeeq S

"""
`gozu config list/edit/delete` - managing saved configs after `gozu init`
has created them, without needing to recreate one from scratch just to
fix a typo or remove a throwaway config. `edit` lives in its own module
(cli/config_cmd/edit.py) given its size; list/delete are small enough to
live here directly.
"""

import questionary
import typer

import config.store as config_store
from cli.config_cmd.edit import edit_command
from cli.prompts import ask_or_exit
from cli.status import error, success

__all__ = ["delete_command", "edit_command", "list_command"]


def list_command() -> None:
    configs = config_store.list_configs()
    if not configs:
        typer.echo("No configs found - run `gozu init` to create one.")
        return

    typer.echo("Configs:")
    for config in configs:
        typer.echo(
            f"  {config['name']:<20} scanner={config['scanner_type']}  mode={config['scanner_mode']:<6}  "
            f"trigger={config['trigger_mode']}"
        )


def delete_command(name: str) -> None:
    """
    Deletes only this one config's own rows (its config_credentials
    cascade via the FK - see config_store.delete_config()) - never the
    ticket_destination it references, even if this was the last config
    using it. A single y/N confirmation, not --wipe's typed-word ritual -
    this is scoped to one config, not the whole store.
    """
    config = config_store.get_config(name)
    if config is None:
        error(f"No config named '{name}' found. Run `gozu config list` to see what's available.")
        raise typer.Exit(code=1)

    typer.echo(f"This will delete config '{name}':")
    typer.echo(f"  scanner:  {config['scanner_type']} ({config['scanner_mode']})")
    typer.echo(f"  trigger:  {config['trigger_mode']}")

    destination_id = config.get("ticket_destination_id")
    if destination_id is not None:
        destination = config_store.get_ticket_destination_by_id(destination_id)
        destination_name = destination["name"] if destination else f"id={destination_id}"
        typer.echo(f"  ticket destination: '{destination_name}' (shared - this destination itself is NOT deleted)")
    else:
        typer.echo("  ticket destination: embedded credentials on this config (not shared with anything else)")

    if not ask_or_exit(questionary.confirm(f"Delete '{name}'?", default=False)):
        typer.echo("Cancelled - nothing was deleted.")
        return

    config_store.delete_config(name)
    success(f"Deleted config '{name}'.")
