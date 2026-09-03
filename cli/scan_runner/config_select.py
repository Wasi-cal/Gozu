"""Resolving which saved config `codescan run` should use."""

import questionary
import typer

import config.store as config_store


def select_config(name: str | None) -> dict:
    """
    A given `name` is looked up directly (clear error if it doesn't
    exist). With no name: errors if none exist ("run `codescan init`
    first"), auto-selects (and announces) the only one if exactly one
    exists, or shows an informed questionary select (name + scanner_mode +
    trigger_mode, not just a bare name list) if there are several.
    """
    if name:
        config = config_store.get_config(name)
        if config is None:
            typer.secho(f"No config named '{name}' found. Run `codescan init` to create one.", fg=typer.colors.RED)
            raise typer.Exit(code=1)
        return config

    configs = config_store.list_configs()
    if not configs:
        typer.secho("No configs found - run `codescan init` first.", fg=typer.colors.RED)
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
        typer.secho(f"Config '{chosen_name}' disappeared before it could be loaded.", fg=typer.colors.RED)
        raise typer.Exit(code=1)
    return config
