# Copyright (c) 2026 Calfus Inc.
# Author: Wasiullah Rafeeq S
# Editor: Prakrit Mohanty

"""
SonarQube Cloud (Free/Premium) credential collection for the init wizard.

Split into structural, one-shot pieces (select_cloud_plan(),
confirm_free_plan_limitation(), show_premium_intro() - these decide/
announce WHICH fields exist and are never revisable, same category as
Local-vs-Cloud itself) and plain value-prompt functions (prompt_sonar_
organization(), prompt_sonar_token_cloud(), prompt_free_branch(),
prompt_premium_branches()) that cli/wizard_engine.py's WizardFields wrap
directly - each accepts a `default` so `gozu config edit` can prefill the
current value, same prompt substance either way.
"""

import questionary
import typer

from cli.help_links import print_help_link
from cli.prompts import ask_or_exit, prompt_text
from cli.status import error, warning

FREE_PLAN_WARNING = (
    "SonarQube Cloud's Free plan only analyzes pull requests once they're merged to main - "
    "never before. This isn't a limitation of how gozu triggers analysis; it's a hard, "
    "server-side restriction on the Free plan itself. Any tickets this config creates will "
    "reflect post-merge analysis only, never pre-merge/PR-time findings."
)


def select_cloud_plan() -> str:
    return ask_or_exit(questionary.select("Free plan or Premium?", choices=["Free", "Premium"]))


def prompt_sonar_organization(default: str = "") -> str:
    print_help_link("sonar_organization")
    return ask_or_exit(questionary.text("SonarQube Cloud organization key:", default=default))


def prompt_sonar_token_cloud(default: str = "") -> str:
    print_help_link("sonar_token_cloud")
    return ask_or_exit(questionary.text("SonarQube Cloud token:", default=default))


def confirm_free_plan_limitation() -> None:
    """
    Free's hard server-side limitation (analysis only happens post-merge
    to main) - shown once, structural, when Free is chosen during init;
    raises typer.Exit if declined. Never revisable (not a WizardField) -
    unlike a value, "un-acknowledging" this mid-review wouldn't mean
    anything.
    """
    typer.echo()
    warning(FREE_PLAN_WARNING)
    understood = ask_or_exit(questionary.confirm("I understand", default=False))
    if not understood:
        error("Aborting - Free plan requires acknowledging this limitation to continue.")
        raise typer.Exit(code=1)


def prompt_free_branch(default: str = "main") -> str:
    return prompt_text("Target branch to track:", default=default).strip() or "main"


def show_premium_intro() -> None:
    typer.echo(
        "\nPremium unlocks real pre-merge, per-branch analysis, delivered via webhook as soon as "
        "SonarQube Cloud finishes analyzing each push."
    )


def prompt_premium_branches(default: str = "main") -> str:
    branches_input = prompt_text(
        "Branches to track (comma-separated - patterns like 'release/*' are supported):", default=default
    )
    return ",".join(b.strip() for b in branches_input.split(",") if b.strip()) or "main"
