"""codescan CLI entrypoint (see pyproject.toml's [project.scripts])."""

import typer

from cli.init_wizard import run_init_wizard
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


@app.command()
def run() -> None:
    """Run a scan using a saved config."""
    typer.echo("`codescan run` is not yet implemented - coming in a later phase.")


@app.command()
def down() -> None:
    """Tear down the local infrastructure stack."""
    typer.echo("`codescan down` is not yet implemented - coming in a later phase.")


if __name__ == "__main__":
    app()
