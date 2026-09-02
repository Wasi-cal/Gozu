"""
`codescan init` - the interactive setup wizard. Provisions .env, checks/
installs prerequisites, walks through scanner + ticket credential
selection, and seeds one row via config_store.create_config(). Ends at "a
named config exists in Postgres" - actually running a scan is a later
phase, not touched here.

Every select/confirm prompt uses questionary (arrow-key menus), not typed
option strings - this is a deliberate, fixed choice given how many
branching selects this wizard has, not a per-prompt toss-up.
"""

import secrets

import questionary
import typer

import config_store
from cli.help_links import print_help_link
from cli.prerequisites import ensure_java
from scanner.client import SCANNER_REGISTRY
from scripts.bootstrap_env import FernetKeySafetyError, bootstrap_env, resolve_ports


def _ask_or_exit(question: questionary.Question) -> str:
    """questionary returns None on Ctrl-C/Ctrl-D - treat that as a clean abort, not a crash."""
    answer = question.ask()
    if answer is None:
        raise typer.Exit(code=1)
    return answer


def _prompt_text(label: str, field: str | None = None, default: str = "") -> str:
    if field:
        print_help_link(field)
    return _ask_or_exit(questionary.text(label, default=default))


def _step_bootstrap_env() -> None:
    typer.secho("Step 1/4: environment (.env)", bold=True)

    ports = resolve_ports()
    typer.echo("Ports that will be used (auto-detected as free - override any of them below):")
    overrides: dict[str, int] = {}
    for name, port in ports.items():
        answer = _ask_or_exit(questionary.text(f"  {name}", default=str(port)))
        overrides[name] = int(answer)

    try:
        new_fields = bootstrap_env(port_overrides=overrides)
    except FernetKeySafetyError as e:
        typer.secho(f"Refusing to continue: {e}", fg=typer.colors.RED)
        raise typer.Exit(code=1) from e

    if new_fields:
        typer.echo("Wrote new .env field(s): " + ", ".join(new_fields))
    else:
        typer.echo(".env already had everything needed - left untouched.")


def _step_ensure_java() -> None:
    typer.secho("Step 2/4: prerequisites", bold=True)
    ensure_java()


def _select_scanner() -> str:
    choices = list(SCANNER_REGISTRY.items())
    if len(choices) == 1:
        scanner_type, label = choices[0]
        typer.echo(f"Only one scanner is registered: {label}")
        return scanner_type

    answer = _ask_or_exit(
        questionary.select(
            "Choose a scanner:",
            choices=[questionary.Choice(title=label, value=scanner_type) for scanner_type, label in choices],
        )
    )
    return answer


def _select_scanner_mode() -> str:
    answer = _ask_or_exit(questionary.select("Local or Cloud?", choices=["Local", "Cloud"]))
    return "local" if answer == "Local" else "cloud"


def _generate_or_prompt_secret(label: str) -> str:
    secret = _ask_or_exit(questionary.text(f"{label} (leave blank to generate one):"))
    if secret:
        return secret
    generated = secrets.token_urlsafe(32)
    typer.secho(f"Generated: {generated}", fg=typer.colors.YELLOW)
    typer.secho("(shown once - it's stored encrypted, this is your only chance to copy it elsewhere)", dim=True)
    return generated


def _collect_local_sonar() -> tuple[dict[str, str], str]:
    """
    Returns (credentials, trigger_mode). Local self-hosted SonarQube is the
    backend the webhook receiver (receiver/app.py) already exists for, so
    trigger_mode defaults to "webhook" here and a webhook secret is
    collected to match receiver/verify_signature.py's HMAC check.
    """
    host_url = _prompt_text("SonarQube host URL:", default="http://localhost:9000")
    token = _prompt_text("SonarQube token:", field="sonar_token_local")
    webhook_secret = _generate_or_prompt_secret("Webhook secret")
    return {"sonar_host_url": host_url, "sonar_token": token, "webhook_secret": webhook_secret}, "webhook"


def _collect_cloud_sonar() -> tuple[dict[str, str], str]:
    """Returns (credentials, trigger_mode)."""
    plan = _ask_or_exit(questionary.select("Free plan or Premium?", choices=["Free", "Premium"]))

    credentials: dict[str, str] = {}
    print_help_link("sonar_organization")
    credentials["sonar_organization"] = _ask_or_exit(questionary.text("SonarQube Cloud organization key:"))
    print_help_link("sonar_token_cloud")
    credentials["sonar_token"] = _ask_or_exit(questionary.text("SonarQube Cloud token:"))

    if plan == "Premium":
        use_webhook = _ask_or_exit(questionary.confirm("Use a webhook?", default=True))
        if use_webhook:
            credentials["webhook_secret"] = _generate_or_prompt_secret("Webhook secret")
            print_help_link("sonar_branch")
            credentials["sonar_branch"] = _prompt_text(
                "Which branch should be scoped for analysis? (branch analysis is Premium-only)",
                default="main",
            )
            return credentials, "webhook"
        return credentials, "direct"

    typer.secho(
        "SonarQube Cloud's Free plan doesn't invoke webhooks, so that trigger isn't offered here.", dim=True
    )
    trigger_mode = _ask_or_exit(
        questionary.select(
            "Choose a trigger mode:",
            choices=[
                questionary.Choice(title="Direct (your own CI)", value="direct"),
                questionary.Choice(title="GitHub Actions signal + poll", value="github_poll"),
                questionary.Choice(title="Scheduled watch polling", value="watch"),
            ],
        )
    )
    return credentials, trigger_mode


def _collect_jira() -> dict[str, str]:
    typer.secho("Jira details", bold=True)
    return {
        "jira_url": _prompt_text("Jira URL (e.g. https://your-domain.atlassian.net):"),
        "jira_email": _prompt_text("Jira account email:", field="jira_email"),
        "jira_api_token": _prompt_text("Jira API token:", field="jira_api_token"),
        "jira_project_key": _prompt_text("Jira project key:", field="jira_project_key"),
    }


def _prompt_config_name() -> str:
    existing_names = {config["name"] for config in config_store.list_configs()}
    while True:
        name = _ask_or_exit(questionary.text("Name this config:")).strip()
        if not name:
            typer.secho("Name can't be empty.", fg=typer.colors.RED)
            continue
        if name in existing_names:
            typer.secho(f"A config named '{name}' already exists - choose another name.", fg=typer.colors.RED)
            continue
        return name


def run_init_wizard() -> None:
    typer.secho("codescan init", bold=True, underline=True)

    _step_bootstrap_env()
    _step_ensure_java()

    typer.secho("Step 3/4: scanner + credentials", bold=True)
    scanner_type = _select_scanner()
    scanner_mode = _select_scanner_mode()

    if scanner_mode == "local":
        credentials, trigger_mode = _collect_local_sonar()
    else:
        credentials, trigger_mode = _collect_cloud_sonar()

    credentials.update(_collect_jira())

    typer.secho("Step 4/4: name this config", bold=True)
    name = _prompt_config_name()

    config_store.create_config(
        name=name,
        scanner_type=scanner_type,
        scanner_mode=scanner_mode,
        ticket_backend="jira",
        trigger_mode=trigger_mode,
        credentials=credentials,
    )

    typer.secho("\nConfig created:", bold=True, fg=typer.colors.GREEN)
    typer.echo(f"  name:           {name}")
    typer.echo(f"  scanner:        {SCANNER_REGISTRY.get(scanner_type, scanner_type)} ({scanner_mode})")
    typer.echo("  ticket backend: jira")
    typer.echo(f"  trigger mode:   {trigger_mode}")
    typer.echo("\n`codescan run` isn't implemented yet - that's a later phase.")
