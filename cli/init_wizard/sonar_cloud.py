"""SonarQube Cloud (Free/Premium) credential collection for the init wizard."""

import questionary
import typer

from cli.help_links import print_help_link
from cli.init_wizard.prompts import ask_or_exit, generate_or_prompt_secret, prompt_text
from cli.init_wizard.sonar_local import prompt_project_key

FREE_PLAN_WARNING = (
    "SonarQube Cloud's Free plan only analyzes pull requests once they're merged to main - "
    "never before. This isn't a limitation of how gozu triggers analysis; it's a hard, "
    "server-side restriction on the Free plan itself. Any tickets this config creates will "
    "reflect post-merge analysis only, never pre-merge/PR-time findings."
)


def _collect_cloud_free() -> tuple[str, str, str]:
    """
    Returns (trigger_mode, sonar_plan, branches). Free's hard server-side
    limitation (analysis only happens post-merge to main) means there's
    exactly one trigger mechanism that can ever apply here - polling via
    `gozu run --watch` - so trigger_mode is set directly, no select
    prompt for it.
    """
    typer.secho(f"\n{FREE_PLAN_WARNING}", fg=typer.colors.YELLOW, bold=True)
    understood = ask_or_exit(questionary.confirm("I understand", default=False))
    if not understood:
        typer.secho("Aborting - Free plan requires acknowledging this limitation to continue.", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    branch = prompt_text("Target branch to track:", default="main").strip() or "main"
    typer.echo("trigger_mode is set to 'watch' automatically - no other mechanism applies on the Free plan.")
    return "watch", "free", branch


def _collect_cloud_premium(credentials: dict[str, str]) -> tuple[str, str, str]:
    """
    Returns (trigger_mode, sonar_plan, branches). Premium's real value is
    pre-merge, per-branch analysis delivered via webhook - that's the only
    mechanism that delivers what Premium is for, so trigger_mode is set
    directly here too, no select prompt.
    """
    typer.echo(
        "\nPremium unlocks real pre-merge, per-branch analysis, delivered via webhook as soon as "
        "SonarQube Cloud finishes analyzing each push."
    )
    branches_input = prompt_text(
        "Branches to track (comma-separated - patterns like 'release/*' are supported):", default="main"
    )
    branches = ",".join(b.strip() for b in branches_input.split(",") if b.strip()) or "main"

    credentials["webhook_secret"] = generate_or_prompt_secret("Webhook secret")
    typer.echo("trigger_mode is set to 'webhook' automatically - it's the only mechanism Premium needs.")
    return "webhook", "premium", branches


def collect_cloud_sonar() -> tuple[dict[str, str], str, str, str, str]:
    """Returns (credentials, trigger_mode, project_key, sonar_plan, branches)."""
    plan = ask_or_exit(questionary.select("Free plan or Premium?", choices=["Free", "Premium"]))

    credentials: dict[str, str] = {}
    print_help_link("sonar_organization")
    credentials["sonar_organization"] = ask_or_exit(questionary.text("SonarQube Cloud organization key:"))
    print_help_link("sonar_token_cloud")
    credentials["sonar_token"] = ask_or_exit(questionary.text("SonarQube Cloud token:"))
    project_key = prompt_project_key()

    if plan == "Free":
        trigger_mode, sonar_plan, branches = _collect_cloud_free()
    else:
        trigger_mode, sonar_plan, branches = _collect_cloud_premium(credentials)

    return credentials, trigger_mode, project_key, sonar_plan, branches
