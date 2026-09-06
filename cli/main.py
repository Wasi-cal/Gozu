# Copyright (c) 2026 Calfus Inc.
# Author: Wasiullah Rafeeq S
# Editor: Prakrit Mohanty

"""gozu CLI entrypoint (see pyproject.toml's [project.scripts])."""

import importlib.metadata
import time

import typer

from cli.init_wizard import run_init_wizard
from cli.scan_runner import run_scan_cycle, select_config
from cli.stack import down as stack_down
from cli.stack import up as stack_up
from cli.status import success, waiting
from scripts.bootstrap_env import load_into_environ

app = typer.Typer(
    name="gozu",
    help=(
        "gozu scans your code with SonarQube and automatically creates Jira tickets for "
        "the vulnerabilities it finds, orchestrated with Temporal so scans, dedupe, and "
        "ticket creation survive crashes and retries. Run `gozu init` first to set everything up."
    ),
    no_args_is_help=True,
)


def _version_callback(value: bool) -> None:
    """
    Reads gozu's version from installed package metadata (pyproject.toml's
    own [project] version at build/install time) - never a hardcoded
    string here that could drift out of sync with it. A checkout run
    without gozu actually installed as a package (no `uv sync`/`pip
    install`, just executing the source directly) has no such metadata at
    all - importlib.metadata.version() raises PackageNotFoundError in
    that case, handled here rather than left to crash.
    """
    if not value:
        return
    try:
        version = importlib.metadata.version("gozu")
        typer.echo(f"gozu {version}")
    except importlib.metadata.PackageNotFoundError:
        typer.echo("gozu (version unknown - not installed as a package)")
    raise typer.Exit()


@app.callback()
def _load_env(
    version: bool = typer.Option(
        False,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Show gozu's version and exit.",
    ),
) -> None:
    """
    Load .env (if it exists yet) into this process's environment before any
    command runs - config.store and friends read POSTGRES_HOST etc from
    os.environ, which a `source .env` in the shell normally provides, but
    gozu can't assume the caller did that.
    """
    load_into_environ()


@app.command()
def init() -> None:
    """
    Set up gozu: an interactive wizard that writes your local .env,
    downloads Java/sonar-scanner if needed, walks you through SonarQube +
    Jira credentials, and saves the result as a named config. Run this
    once per project/scanner-ticket combination before anything else - run
    it again to add another config.
    """
    run_init_wizard()


def _print_summary(summary: dict) -> None:
    created = summary["created"]
    skipped = summary["skipped"]
    success(
        f"Done: created {len(created)} ticket(s), skipped {len(skipped)} already-ticketed finding(s) "
        f"(SonarQube task {summary['ce_task_id']})"
    )
    for entry in created:
        typer.echo(f"  created {entry['ticket_key']} for finding {entry['finding_key']}")
    for finding_key in skipped:
        typer.echo(f"  skipped finding {finding_key} (ticket already exists)")


@app.command()
def run(
    config: str = typer.Option(
        None, "--config", "-c", help="Name of the saved config to use (auto-selects it if you only have one)."
    ),
    watch: bool = typer.Option(
        False,
        "--watch",
        help="Keep running the scan cycle on a repeating interval instead of once and exiting - "
        "for backends with no push-based trigger (e.g. SonarQube Cloud Free).",
    ),
    interval: int = typer.Option(
        300, "--interval", help="Seconds to wait between scan cycles - only meaningful together with --watch."
    ),
    path: str = typer.Option(".", "--path", help="Path to the code to scan - defaults to the current directory."),
) -> None:
    """
    Run sonar-scanner against your code, wait for SonarQube to finish
    analyzing it server-side, then create a Jira ticket for each new
    finding (skipping ones already ticketed). Requires a config from
    `gozu init` and the stack to be up (`gozu up`).
    """
    selected = select_config(config)

    if not watch:
        summary = run_scan_cycle(selected, path)
        _print_summary(summary)
        return

    waiting(f"Watching every {interval}s - Ctrl+C to stop.")
    try:
        while True:
            summary = run_scan_cycle(selected, path)
            _print_summary(summary)
            time.sleep(interval)
    except KeyboardInterrupt:
        typer.echo("\nStopping.")


@app.command()
def up() -> None:
    """
    Start the local Docker services gozu needs: Postgres and Temporal
    always, plus SonarQube and/or the webhook receiver if one of your
    configs actually needs them. Run this before `gozu run`, and prints a
    status summary of every service once it's done.
    """
    stack_up()


@app.command()
def down(
    wipe: bool = typer.Option(
        False,
        "--wipe",
        help="DESTRUCTIVE: after stopping, also permanently delete every config, ticket destination, "
        "dedupe record, and volume (a Postgres backup is taken first, and you'll be asked to confirm).",
    ),
) -> None:
    """
    Stop the Docker services `gozu up` started. Containers stop but data
    persists (configs, Postgres/SonarQube volumes) unless you pass --wipe,
    which deletes it all permanently.
    """
    stack_down(wipe)


if __name__ == "__main__":
    app()
