# Copyright (c) 2026 Calfus Inc.
# Author: Wasiullah Rafeeq S
# Editor: Prakrit Mohanty

"""Input model for ScanToTicketWorkflow."""

from pydantic import BaseModel


class SonarToJiraInput(BaseModel):
    project_key: str
    task_id: str | None = None

    # A specific config's scanner/ticket backend + credentials, as selected
    # by `gozu run` (cli/scan_runner/) - config.store.get_config()'s
    # scanner_type/scanner_mode/ticket_backend/credentials shape, passed
    # straight through. All default to the legacy single-global-config
    # values so the webhook receiver (receiver/starter.py), which has no
    # concept of a "config" and never sets these, is unaffected: an empty
    # `credentials` tells every activity to fall back to its env-var-based
    # get_scanner_client()/get_ticket_client() exactly as before.
    scanner_type: str = "sonarqube"
    scanner_mode: str = "local"
    ticket_backend: str = "jira"
    credentials: dict[str, str] = {}

    # The git branch of whatever was actually scanned. Computed on the
    # *host* by cli/scan_runner.py (which has real git access to the
    # scanned path) and passed through here - the worker container has no
    # .git at all (excluded via .dockerignore), so fetch_findings_activity
    # can never determine this by introspecting its own filesystem. None
    # (the webhook receiver's default - it has no host path to inspect
    # either) falls back to that same, structurally-limited in-container
    # attempt for backward compatibility, not because it's expected to work.
    branch: str | None = None
