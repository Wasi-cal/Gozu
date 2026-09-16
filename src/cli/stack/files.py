# Copyright (c) 2026 Calfus Inc.
# Author: Wasiullah Rafeeq S

"""
Materializes the Docker stack (docker-compose.yml, Dockerfile, db/, and
the Python source tree the worker/receiver images build from) into
~/.gozu/stack/ - the directory every `docker compose` invocation in
src/cli/stack/ runs against.

A pip install has none of this on disk next to the installed package -
only the entry point and the packages listed in pyproject.toml's wheel
`packages` land in site-packages, and the images' `build: .` step needs a
real source tree to build from. `ensure_stack_files()` (called by `gozu
init`) reads the bundled copies via importlib.resources and writes them
into ~/.gozu/stack/, mirroring the repo-root layout.

A dedicated subdirectory, not flat under ~/.gozu/, since a user may
separately keep an unrelated `temporal` (the Temporal CLI binary) there -
flattening would silently merge gozu's files into a directory it doesn't
own. `gozu up`/`down` only check this already happened
(require_initialized()); they never materialize on their own.
"""

import importlib.resources
from importlib.resources.abc import Traversable
from pathlib import Path

import typer

from cli.status import error
from scripts.paths import STACK_DIR

_ASSETS_PACKAGE = "cli.stack._stackfiles"

# Top-level files bundled verbatim under src/cli/stack/_stackfiles/ (see
# pyproject.toml's wheel `packages`).
_STATIC_FILES = [
    "docker-compose.yml",
    "Dockerfile",
    "pyproject.toml",
    "uv.lock",
    "alembic.ini",
]

# db/ is copied as a whole tree, like _SOURCE_PACKAGES below, so a future
# Alembic revision needs no change here to reach ~/.gozu/stack/.
#
# Named db/migrations/, not db/alembic/: a directory literally named
# `alembic` at this nesting depth gets silently dropped from the wheel by
# hatchling entirely, a name collision with the installed `alembic` PyPI
# package. alembic.ini (a file, not a directory) is unaffected.
_DIRECTORY_ASSETS = ["db"]

# Every package the worker/receiver images need on disk to build, copied
# from the running installation (not a second bundled copy), under src/ to
# mirror the repo-root layout pyproject.toml's `packages` expects.
_SOURCE_PACKAGES = ["core", "scanner", "ticket", "temporal", "receiver", "cli", "scripts", "config", "llm"]

_SKIP_DIR_NAMES = {"__pycache__", "_stackfiles"}


def _copy_tree(source: Traversable, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    for item in source.iterdir():
        if item.name in _SKIP_DIR_NAMES:
            continue
        target = dest / item.name
        if item.is_dir():
            _copy_tree(item, target)
        else:
            target.write_bytes(item.read_bytes())


def ensure_stack_files() -> Path:
    """Write docker-compose.yml/Dockerfile/sql/alembic.ini/alembic/ and the source tree into ~/.gozu/stack/, returning it. Safe to call repeatedly - always re-copies to match whatever gozu version is installed; never touches .env or ~/.gozu/'s other subdirs (jre/, sonar-scanner/, backups/)."""
    STACK_DIR.mkdir(parents=True, exist_ok=True)
    assets = importlib.resources.files(_ASSETS_PACKAGE)

    for relative_path in _STATIC_FILES:
        dest = STACK_DIR / relative_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        source = assets
        for part in relative_path.split("/"):
            source = source / part
        dest.write_bytes(source.read_bytes())

    for relative_path in _DIRECTORY_ASSETS:
        _copy_tree(assets / relative_path, STACK_DIR / relative_path)

    for package in _SOURCE_PACKAGES:
        _copy_tree(importlib.resources.files(package), STACK_DIR / "src" / package)

    return STACK_DIR


def is_initialized() -> bool:
    """Whether `gozu init` has ever materialized the stack - checked by `gozu up`/`down` before doing anything else."""
    return (STACK_DIR / ".env").exists() and (STACK_DIR / "docker-compose.yml").exists()


def require_initialized() -> None:
    if not is_initialized():
        error("Stack not initialized - run `gozu init` first.")
        raise typer.Exit(code=1)
