"""
`gozu up`/`gozu down` - bringing the local Docker Compose
infrastructure up/down, with which profiles to activate determined by
whatever configs currently exist (see cli/init_wizard.py for how those
get created).
"""

import secrets
import subprocess

import typer

import config.store as config_store

# Fixed inside the Docker network (docker-compose.yml's receiver service
# always listens on 5000 internally) - RECEIVER_PORT (.env) is only the
# host-side port mapping, irrelevant to service-to-service URLs like the
# one printed below.
_INTERNAL_RECEIVER_PORT = 5000


def _required_profiles(configs: list[dict]) -> set[str]:
    profiles = set()
    for config in configs:
        if config["scanner_mode"] == "local":
            profiles.add("sonarqube-local")
        if config["trigger_mode"] == "webhook":
            profiles.add("webhook")
    return profiles


def _ensure_webhook_secrets(configs: list[dict]) -> None:
    """Generate + persist a webhook_secret for any webhook-mode config that doesn't have one yet."""
    for config in configs:
        if config["trigger_mode"] != "webhook":
            continue

        full = config_store.get_config(config["name"])
        if full is None or "webhook_secret" in full["credentials"]:
            continue

        secret = secrets.token_urlsafe(32)
        config_store.set_credential(config["name"], "webhook_secret", secret)
        typer.secho(f"Generated a webhook secret for '{config['name']}':", fg=typer.colors.YELLOW)
        typer.secho(f"  {secret}", bold=True)
        typer.secho("  (shown once - it's stored encrypted, this is your only chance to copy it elsewhere)", dim=True)


def _print_webhook_urls(configs: list[dict]) -> None:
    for config in configs:
        if config["trigger_mode"] != "webhook":
            continue

        url = f"http://receiver:{_INTERNAL_RECEIVER_PORT}/webhooks/sonarqube/{config['name']}"
        if config["scanner_mode"] == "local":
            typer.echo(f"\nConfigure this webhook URL in SonarQube for '{config['name']}': {url}")
        else:
            typer.echo(
                f"\n'{config['name']}' is Cloud + webhook mode - {url} only resolves inside this "
                "Docker network. You'll need to expose it externally yourself (a tunnel, a public "
                "deploy, etc) for SonarQube Cloud to actually reach it - gozu doesn't set that "
                "up automatically."
            )


def _ensure_postgres_up() -> None:
    """
    list_configs() below needs a live Postgres connection - but postgres is
    itself one of the services `up` is meant to start, so it has to come up
    (and actually finish its healthcheck, not just start the container)
    before any config_store call. Only matters when postgres isn't already
    running (e.g. right after `gozu down`, which stops every container) -
    a no-op if it's already up and healthy.
    """
    result = subprocess.run(["docker", "compose", "up", "-d", "--wait", "postgres"], check=False)
    if result.returncode != 0:
        typer.secho("Failed to start postgres.", fg=typer.colors.RED)
        raise typer.Exit(code=result.returncode)


def up() -> None:
    _ensure_postgres_up()

    configs = config_store.list_configs()
    if not configs:
        typer.secho(
            "No configs found - run `gozu init` first. Bringing up always-on services only.",
            fg=typer.colors.YELLOW,
        )

    profiles = _required_profiles(configs)
    _ensure_webhook_secrets(configs)

    # --profile is a top-level `docker compose` flag, not an `up` option -
    # it has to come before the subcommand.
    command = ["docker", "compose"]
    for profile in sorted(profiles):
        command += ["--profile", profile]
    command += ["up", "-d"]

    typer.echo("Running: " + " ".join(command))
    result = subprocess.run(command, check=False)
    if result.returncode != 0:
        typer.secho("docker compose up failed.", fg=typer.colors.RED)
        raise typer.Exit(code=result.returncode)

    typer.echo("\nStatus:")
    subprocess.run(["docker", "compose", "ps"], check=False)
    if "sonarqube-local" in profiles:
        typer.secho(
            "\nsonarqube can take 30-60s+ to show healthy - check `docker compose ps` again shortly.", dim=True
        )

    _print_webhook_urls(configs)


def down() -> None:
    """Stop the stack - containers stop, volumes/data persist. No destructive/--wipe option here."""
    typer.echo("Running: docker compose stop")
    result = subprocess.run(["docker", "compose", "stop"], check=False)
    if result.returncode != 0:
        raise typer.Exit(code=result.returncode)
