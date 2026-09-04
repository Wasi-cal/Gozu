# Copyright (c) 2026 Calfus Inc.
# Author: Wasiullah Rafeeq S

"""Webhook-secret generation + URL printing for `gozu up`."""

import secrets

import typer

import config.store as config_store

# Fixed inside the Docker network (docker-compose.yml's receiver service
# always listens on 5000 internally) - RECEIVER_PORT (.env) is only the
# host-side port mapping, irrelevant to service-to-service URLs like the
# one printed below.
_INTERNAL_RECEIVER_PORT = 5000


def ensure_webhook_secrets(configs: list[dict]) -> None:
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


def print_webhook_urls(configs: list[dict]) -> None:
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
