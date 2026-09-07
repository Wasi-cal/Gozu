# Copyright (c) 2026 Calfus Inc.
# Author: Wasiullah Rafeeq S

"""
Materializes the Docker stack (docker-compose.yml, Dockerfile, sql/init.sql,
and the Python source tree the worker/receiver images build from) into
~/.gozu/stack/ - the directory every `docker compose` invocation in
cli/stack/ runs against (see profiles.py, wipe.py, backup.py).

This exists because a pip install has none of these on disk next to the
installed package - only the `[project.scripts]` entry point and the
Python source dirs listed in pyproject.toml's wheel `include` land in
site-packages. `docker-compose.yml`/`Dockerfile`/`sql/init.sql` were never
bundled as package data at all, and the worker/receiver images' `build: .`
step needs a real source tree (pyproject.toml, uv.lock, and the packages
COPY . . pulls in) to build from - none of which exists anywhere once
gozu is installed from a wheel rather than run from a repo checkout.

`ensure_stack_files()` is what `gozu init` calls to fix that: it reads the
bundled copies (via importlib.resources - works whether installed from a
wheel, an editable install, or a zipped package) and writes them into
~/.gozu/stack/, mirroring the repo-root layout exactly so docker-compose.yml's
`env_file: .env` and `build: .` keep resolving the same way they always
did. It's a dedicated subdirectory, not flat under ~/.gozu/, deliberately -
this repo has a `temporal/` source package, and ~/.gozu/ is also where a
user may separately keep an unrelated `temporal` (the Temporal CLI binary,
common enough as a standalone dev tool) - flattening the two would silently
merge gozu's package files into a directory it doesn't own. `gozu up`/
`down` only ever check that this already happened (require_initialized())
- they don't materialize on their own, so a `gozu up` before any `gozu
init` fails with a direct message instead of a confusing traceback about
a missing .env.
"""

import importlib.resources
from importlib.resources.abc import Traversable
from pathlib import Path

import typer

from cli.status import error
from scripts.paths import STACK_DIR

_ASSETS_PACKAGE = "cli.stack._stackfiles"

# Top-level files bundled verbatim under cli/stack/_stackfiles/ (see
# pyproject.toml's [tool.hatch.build.targets.wheel.force-include]).
_STATIC_FILES = ["docker-compose.yml", "Dockerfile", "pyproject.toml", "uv.lock", "sql/init.sql"]

# Every package the worker/receiver Docker images need on disk to build
# (pyproject.toml's wheel `include` list) - already installed wherever
# gozu itself is installed, so these are copied from the running
# installation, not from a second bundled copy.
_SOURCE_PACKAGES = ["core", "scanner", "ticket", "temporal", "receiver", "cli", "scripts", "config"]

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
    """Write docker-compose.yml/Dockerfile/sql/init.sql and the source tree into ~/.gozu/stack/, returning it. Safe to call repeatedly - always re-copies to match whatever gozu version is installed; never touches .env or ~/.gozu/'s other subdirs (jre/, sonar-scanner/, backups/)."""
    STACK_DIR.mkdir(parents=True, exist_ok=True)
    assets = importlib.resources.files(_ASSETS_PACKAGE)

    for relative_path in _STATIC_FILES:
        dest = STACK_DIR / relative_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        source = assets
        for part in relative_path.split("/"):
            source = source / part
        dest.write_bytes(source.read_bytes())

    for package in _SOURCE_PACKAGES:
        _copy_tree(importlib.resources.files(package), STACK_DIR / package)

    return STACK_DIR


def is_initialized() -> bool:
    """Whether `gozu init` has ever materialized the stack - checked by `gozu up`/`down` before doing anything else."""
    return (STACK_DIR / ".env").exists() and (STACK_DIR / "docker-compose.yml").exists()


def require_initialized() -> None:
    if not is_initialized():
        error("Stack not initialized - run `gozu init` first.")
        raise typer.Exit(code=1)
