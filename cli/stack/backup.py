# Copyright (c) 2026 Calfus Inc.
# Author: Wasiullah Rafeeq S
#
# Depends on: PostgreSQL - data storage

"""
Pre-`--wipe` Postgres backup - added after a real incident: a fully
confirmed, deliberately-executed `--wipe` destroyed real dev
configs/credentials with nothing to restore from, since no backup existed
at the time. Postgres only (configs, ticket_destinations, claims) -
SonarQube's volume is a deliberate exclusion, not an oversight.
"""

import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

from scripts.paths import GOZU_HOME

BACKUP_DIR = GOZU_HOME / "backups"

# How long a backup sticks around before the next wipe prunes it - a
# named constant so it's easy to tune later, not a magic number buried
# in prune_old_backups().
_RETENTION_DAYS = 7


def next_backup_path() -> Path:
    """
    A fresh, not-yet-created path for the next backup - computed once and
    reused for both the pre-confirmation preview and the actual dump, so
    the path shown to the user is exactly the path that gets written
    (not recomputed with a different timestamp after they answer).
    Colons are filesystem-unsafe on some platforms, so this isn't
    strictly ISO8601 - it's ISO8601 with ':' replaced by '-'.
    """
    timestamp = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")
    return BACKUP_DIR / f"wipe-{timestamp}.sql"


def prune_old_backups() -> None:
    """Delete any backup older than _RETENTION_DAYS. Piggybacks on a wipe actually running - no separate scheduled job."""
    if not BACKUP_DIR.is_dir():
        return
    cutoff = time.time() - _RETENTION_DAYS * 86400
    for path in BACKUP_DIR.glob("wipe-*.sql"):
        if path.stat().st_mtime < cutoff:
            path.unlink()


def create_backup(path: Path, stack_dir: Path) -> None:
    """
    Dumps the running postgres container's database to `path` (see
    next_backup_path()). Reads POSTGRES_USER/POSTGRES_DB from the
    container's own environment (the same values docker-compose.yml set
    from .env), not the host's, so this doesn't depend on the calling
    shell having sourced .env. Prunes old backups first, per
    prune_old_backups(). `stack_dir` is the materialized docker-compose.yml's
    directory (see cli/stack/files.py's ensure_stack_files()) - `docker
    compose exec` resolves the compose file from cwd, not this file's
    location.
    """
    prune_old_backups()
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)

    command = ["docker", "compose", "exec", "-T", "postgres", "sh", "-c", 'pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB"']
    with path.open("w") as f:
        result = subprocess.run(command, stdout=f, stderr=subprocess.PIPE, text=True, check=False, cwd=stack_dir)

    if result.returncode != 0:
        path.unlink(missing_ok=True)
        raise RuntimeError(f"pg_dump failed: {result.stderr}")
