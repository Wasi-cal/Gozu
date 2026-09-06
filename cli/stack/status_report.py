# Copyright (c) 2026 Calfus Inc.
# Author: Wasiullah Rafeeq S

"""`gozu up`'s own status summary - so knowing what's running never requires a separate `docker compose ps`/`docker ps` (and specifically surfaces "unhealthy" distinctly, not just "running")."""

import json
import subprocess
from pathlib import Path

import typer

from cli.stack.profiles import profile_flags
from cli.status import ERROR, SUCCESS, WARNING, error


def _status_symbol(state: str, health: str) -> str:
    """
    ✅ only for a genuinely good state (running, and healthy or no
    healthcheck at all); ❌ for anything actually down (not running) OR
    explicitly reported unhealthy - these are surfaced identically as
    failures since either one means "this isn't working right now",
    distinctly from ⚠️ "still starting, not confirmed either way yet".
    """
    if state != "running":
        return ERROR
    if health == "unhealthy":
        return ERROR
    if health in ("", "healthy"):
        return SUCCESS
    return WARNING  # "starting", or any future Health value not yet known here


def print_stack_status(stack_dir: Path, profiles: set[str]) -> None:
    """Query docker compose directly (not the caller's own cached idea of what should be running) and print one line per container: status symbol, name, state/health, ports."""
    command = ["docker", "compose", *profile_flags(profiles), "ps", "--all", "--format", "json"]
    result = subprocess.run(command, capture_output=True, text=True, check=False, cwd=stack_dir)
    if result.returncode != 0 or not result.stdout.strip():
        error(f"Couldn't read stack status: {result.stderr.strip()}")
        return

    typer.echo("\nStatus:")
    for line in result.stdout.strip().splitlines():
        container = json.loads(line)
        service = container.get("Service", "?")
        state = container.get("State", "")
        health = container.get("Health", "")
        ports = container.get("Ports", "") or "-"
        symbol = _status_symbol(state, health)

        status_text = state if not health else f"{state} ({health})"
        typer.echo(f"  {symbol} {service:<12} {status_text:<22} {ports}")
