"""codescan CLI entrypoint (see pyproject.toml's [project.scripts])."""

import time

import typer

from cli.init_wizard import run_init_wizard
from cli.scan_runner import run_scan_cycle, select_config
from scripts.bootstrap_env import load_into_environ

app = typer.Typer(
    name="codescan",
    help="Scan code with SonarQube and auto-create Jira tickets for vulnerabilities.",
)


@app.callback()
def _load_env() -> None:
    """
    Load .env (if it exists yet) into this process's environment before any
    command runs - config_store and friends read POSTGRES_HOST etc from
    os.environ, which a `source .env` in the shell normally provides, but
    codescan can't assume the caller did that.
    """
    load_into_environ()


@app.command()
def init() -> None:
    """Interactively provision .env, check prerequisites, and save a scanner/ticket config."""
    run_init_wizard()


def _print_summary(summary: dict) -> None:
    created = summary["created"]
    skipped = summary["skipped"]
    typer.secho(
        f"Done: created {len(created)} ticket(s), skipped {len(skipped)} already-ticketed finding(s) "
        f"(SonarQube task {summary['ce_task_id']})",
        fg=typer.colors.GREEN,
        bold=True,
    )
    for entry in created:
        typer.echo(f"  created {entry['ticket_key']} for finding {entry['finding_key']}")
    for finding_key in skipped:
        typer.echo(f"  skipped finding {finding_key} (ticket already exists)")


@app.command()
def run(
    config: str = typer.Option(None, "--config", "-c", help="Name of the config to use (auto-selects if only one exists)."),
    watch: bool = typer.Option(False, "--watch", help="Loop the scan cycle on an interval instead of running once."),
    interval: int = typer.Option(300, "--interval", help="Seconds between cycles - only meaningful with --watch."),
    path: str = typer.Option(".", "--path", help="Path to scan."),
) -> None:
    """Scan, wait for SonarQube to finish processing, then create Jira tickets for new findings."""
    selected = select_config(config)

    if not watch:
        summary = run_scan_cycle(selected, path)
        _print_summary(summary)
        return

    typer.echo(f"Watching every {interval}s - Ctrl+C to stop.")
    try:
        while True:
            summary = run_scan_cycle(selected, path)
            _print_summary(summary)
            time.sleep(interval)
    except KeyboardInterrupt:
        typer.echo("\nStopping.")


@app.command()
def down() -> None:
    """Tear down the local infrastructure stack."""
    typer.echo("`codescan down` is not yet implemented - coming in a later phase.")


if __name__ == "__main__":
    app()
