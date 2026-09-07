# Copyright (c) 2026 Calfus Inc.
# Author: Wasiullah Rafeeq S
# Editor: Prakrit Mohanty

"""gozu CLI entrypoint (see pyproject.toml's [project.scripts])."""

import importlib.metadata
import sys
import time

import typer

from cli.config_cmd import delete_command, edit_command, list_command
from cli.crash_handler import handle_unexpected_exception
from cli.init_wizard import run_init_wizard
from cli.report import render_run_report
from cli.scan_runner import run_scan_cycle, select_config
from cli.stack import down as stack_down
from cli.stack import ports as stack_ports
from cli.stack import status as stack_status
from cli.stack import up as stack_up
from cli.status import waiting
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

config_app = typer.Typer(
    name="config",
    help="Manage saved configs after `gozu init` has created them - list what exists, fix a value without recreating one from scratch, or remove a throwaway config.",
    no_args_is_help=True,
)
app.add_typer(config_app, name="config")


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


@app.command()
def run(
    config: str = typer.Option(
        None,
        "--config",
        "-c",
        help="Name of the saved config to use (auto-selects it if you only have one). A name that "
        "doesn't match offers a picker of what actually exists instead of just failing.",
    ),
    watch: bool = typer.Option(
        False,
        "--watch",
        "-w",
        help="Keep running the scan cycle on a repeating interval instead of once and exiting - "
        "for backends with no push-based trigger (e.g. SonarQube Cloud Free).",
    ),
    interval: int = typer.Option(
        300,
        "--interval",
        "-i",
        help="Seconds to wait between scan cycles - only meaningful together with --watch/-w.",
    ),
    path: str = typer.Option(
        ".", "--path", "-p", help="Path to the code to scan - defaults to the current directory."
    ),
    skip_unchanged: bool = typer.Option(
        False,
        "--skip-unchanged",
        "-s",
        help="Skip the scan/fetch/create-tickets sequence entirely when the scan path is a git repo "
        "whose HEAD commit and clean/dirty working-tree state exactly match this config's last "
        "successful run - auto-close reconciliation still runs every cycle regardless. Only helps a "
        "committed-and-clean checkout: a non-git directory, or one whose caller never commits, gets no "
        "benefit and just scans normally, as if this flag were never passed. Off by default.",
    ),
    ticket_cap: int = typer.Option(
        None,
        "--ticket-cap",
        "-t",
        help="Override the per-run cap on genuinely new tickets for THIS invocation only - never written to "
        "the saved config. Takes priority over the config's own persistent default (set via "
        "`gozu config edit`); with neither set, falls back to the built-in default (30).",
    ),
) -> None:
    """
    Run sonar-scanner against your code, wait for SonarQube to finish
    analyzing it server-side, then create a Jira ticket for each new
    finding (skipping ones already ticketed). Requires a config from
    `gozu init` and the stack to be up (`gozu up`). Prints a summary
    report (tickets created/skipped/deferred/auto-closed, run duration)
    once every step has finished.
    """
    selected = select_config(config)

    if not watch:
        start = time.monotonic()
        ce_task_id, branches, ticket_result = run_scan_cycle(selected, path, skip_unchanged, ticket_cap)
        render_run_report(selected["name"], branches, ce_task_id, ticket_result, time.monotonic() - start)
        return

    waiting(f"Watching every {interval}s - Ctrl+C to stop.")
    try:
        while True:
            start = time.monotonic()
            ce_task_id, branches, ticket_result = run_scan_cycle(selected, path, skip_unchanged, ticket_cap)
            render_run_report(selected["name"], branches, ce_task_id, ticket_result, time.monotonic() - start)
            time.sleep(interval)
    except KeyboardInterrupt:
        typer.echo("\nStopping.")


@app.command()
def up(
    config: str = typer.Option(
        None,
        "--config",
        "-c",
        help="Only start what this one saved config needs (still starts Postgres/Temporal/worker "
        "regardless) - omit to start whatever every saved config needs, as before. A name that "
        "doesn't match offers a picker of what actually exists instead of just failing.",
    ),
) -> None:
    """
    Start the local Docker services gozu needs: Postgres and Temporal
    always, plus SonarQube and/or the webhook receiver if one of your
    configs actually needs them. Run this before `gozu run`, and prints a
    status summary of every service once it's done.
    """
    stack_up(config)


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


@app.command()
def status() -> None:
    """
    Show the current state of every Docker service gozu manages (Postgres,
    Temporal, SonarQube, worker, webhook receiver) - healthy, unhealthy,
    starting, or not running - without starting or changing anything.
    Safe to run any time, including before `gozu init`/`gozu up` have ever
    run, or after `gozu down` has stopped everything.
    """
    stack_status()


@app.command()
def ports() -> None:
    """
    Show which host port each service actually resolved to. `gozu init`
    auto-picks the next free port past each default when something else
    is already using it, so a service's real port can differ from what
    you'd expect - this is where to check.
    """
    stack_ports()


@config_app.command("list")
def config_list() -> None:
    """List every saved config: name, scanner type/mode, and trigger mode - no secrets."""
    list_command()


@config_app.command("edit")
def config_edit(
    name: str = typer.Argument(
        ..., help="Name of the config to edit. A name that doesn't match offers a picker of what actually exists."
    ),
) -> None:
    """
    Review and fix an existing config's values (SonarQube project key,
    branches, tokens, Jira details, ...) without recreating it from
    scratch. scanner type/mode and trigger mode can't be changed here -
    those are decided once at `gozu init` time.
    """
    edit_command(name)


@config_app.command("delete")
def config_delete(
    name: str = typer.Argument(
        ..., help="Name of the config to delete. A name that doesn't match offers a picker of what actually exists."
    ),
) -> None:
    """
    Permanently delete one saved config (a single yes/no confirmation,
    not --wipe's typed-word ritual - this only affects one config, not
    the whole store). Never deletes a shared ticket destination this
    config referenced, even if it was the last one using it.
    """
    delete_command(name)


def main() -> None:
    """
    The actual pyproject.toml [project.scripts] entry point (not `app`
    itself) - the only place that wraps the whole CLI invocation in a
    single top-level handler for a genuinely unexpected exception.

    Click's own `app()` call (standalone_mode, the default) already
    converts every deliberate exit path - typer.Exit (--version, every
    "config not found"/"not initialized" message, wipe confirmation,
    subprocess exit-code propagation, ...) and Abort - into a SystemExit
    before it ever reaches this function; KeyboardInterrupt is likewise
    already fully handled at its two actual sources (`run --watch`'s own
    try/except, cli/stack/cleanup.py's InterruptCleanup) before it can
    propagate this far. So both are simply let through unchanged here -
    only a real, unclassified exception (a bug, an unwrapped error from
    somewhere deep in a backend call) falls through to
    handle_unexpected_exception().
    """
    try:
        app()
    except (SystemExit, KeyboardInterrupt):
        raise
    except Exception as e:
        handle_unexpected_exception(e)
        sys.exit(1)


if __name__ == "__main__":
    main()
