# Copyright (c) 2026 Calfus Inc.
# Author: Wasiullah Rafeeq S

"""
The one host-side state directory every part of gozu shares: managed
prerequisites (cli/prerequisites/java.py, sonar_scanner.py), the
materialized Docker stack + .env (cli/stack/files.py, scripts/env_ports.py),
and pre-wipe backups (cli/stack/backup.py). A single constant so all of
them agree on where it is, regardless of whether gozu is running from a
repo checkout or a pip install.
"""

from pathlib import Path

GOZU_HOME = Path.home() / ".gozu"

# The materialized Docker stack (docker-compose.yml, Dockerfile, .env, and
# the source tree the worker/receiver images build from - see
# cli/stack/files.py) lives in its own subdirectory, not flat under
# GOZU_HOME - a source package this repo happens to have (e.g. `temporal`)
# would otherwise collide with an unrelated same-named directory a user
# already has under GOZU_HOME for something else entirely.
STACK_DIR = GOZU_HOME / "stack"
