# Copyright (c) 2026 Calfus Inc.
# Author: Prakrit Mohanty

"""Ensuring the `semgrep` CLI is available on the host."""

import shutil
import subprocess

import typer

from cli.status import success, waiting


def check_semgrep() -> bool:
    """True if a working `semgrep` is found on PATH."""
    binary = shutil.which("semgrep")
    if binary is None:
        return False
    try:
        result = subprocess.run([binary, "--version"], capture_output=True, text=True, check=False)
        return result.returncode == 0
    except OSError:
        return False


def ensure_semgrep() -> str:
    """
    Return a path to a usable `semgrep` binary, installing one if needed.

    Unlike ensure_sonar_scanner() (cli/prerequisites/sonar_scanner.py),
    this doesn't download a standalone archive into ~/.gozu/ - Semgrep
    doesn't publish one. `uv tool install semgrep` (uv's `pipx` equivalent)
    is used instead, consistent with this project's "packaging is uv, not
    pip" stance (see CLAUDE.md) - it installs semgrep into its own isolated
    venv and puts the `semgrep` command on PATH.
    """
    if check_semgrep():
        binary = shutil.which("semgrep")
        if binary is None:
            raise RuntimeError("check_semgrep() succeeded but no binary could be located")
        return binary

    typer.echo("semgrep CLI wasn't found on this machine.")
    waiting("Installing semgrep via `uv tool install semgrep` ...")

    result = subprocess.run(["uv", "tool", "install", "semgrep"], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(
            f"`uv tool install semgrep` failed with status {result.returncode}.\n"
            f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
        )

    binary = shutil.which("semgrep")
    if binary is None:
        raise RuntimeError(
            "`uv tool install semgrep` succeeded but `semgrep` still isn't on PATH - "
            "you may need to run `uv tool update-shell` and restart your shell."
        )

    success(f"semgrep ready: {binary}")
    return binary
