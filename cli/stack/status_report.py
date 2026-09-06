# Copyright (c) 2026 Calfus Inc.
# Author: Wasiullah Rafeeq S

"""`gozu up`/`gozu status`'s shared status summary - so knowing what's running never requires a separate `docker compose ps`/`docker ps` (and specifically surfaces "unhealthy" distinctly, not just "running")."""

from pathlib import Path

import typer

from cli.stack.profiles import service_state, services_for_profiles
from cli.status import ERROR, SUCCESS, WARNING


def _status_symbol(state: str, health: str) -> str:
    """
    SUCCESS only for a genuinely good state (running, and healthy or no
    healthcheck at all); ERROR for anything actually down (not running -
    including never created at all, which reports as state="") OR
    explicitly reported unhealthy - these are surfaced identically as
    failures since either one means "this isn't working right now",
    distinctly from WARNING - "still starting, not confirmed either way yet".
    """
    if state != "running":
        return ERROR
    if health == "unhealthy":
        return ERROR
    if health in ("", "healthy"):
        return SUCCESS
    return WARNING  # "starting", or any future Health value not yet known here


def print_stack_status(stack_dir: Path, profiles: set[str]) -> None:
    """
    Looks up each expected service (services_for_profiles(profiles))
    individually via service_state() - not one bulk `docker compose ps`
    dump - specifically so a service that was never created at all (a
    perfectly normal state: fresh after `gozu init`, or after `gozu down`)
    still gets its own printed line (ERROR, "not created") instead of the
    whole summary being skipped or treated as an error. Called both right
    after `gozu up` brings things up, and standalone by `gozu status` at
    any time, including when nothing has ever been started.
    """
    typer.echo("\nStatus:")
    for service in services_for_profiles(profiles):
        state_dict = service_state(stack_dir, service) or {}
        state = state_dict.get("State", "")
        health = state_dict.get("Health", "")
        ports = state_dict.get("Ports", "") or "-"
        symbol = _status_symbol(state, health)

        if not state:
            status_text = "not created"
        elif health:
            status_text = f"{state} ({health})"
        else:
            status_text = state
        typer.echo(f"  {symbol} {service:<12} {status_text:<22} {ports}")
