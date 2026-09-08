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
    #
    # This value ALSO scopes fetch_findings_activity's actual scanner
    # query (see its own docstring) - so it's deliberately left None for
    # any config SonarQube itself can't/won't branch-scope for (self-hosted
    # Community Build rejects sonar.branch.name outright; Cloud Free's API
    # rejects querying anything but "main" - see
    # cli/scan_runner/__init__.py's own comment on this exact restriction).
    branch: str | None = None

    # The git branch to LABEL tickets/findings with (finding.branch,
    # shown in the Jira description) - always the host's real git branch
    # (cli/scan_runner/__init__.py's detect_git_branch()) regardless of
    # scanner_mode/sonar_plan, unlike `branch` above. Ticket labeling
    # doesn't share `branch`'s restriction: SonarQube never needs to agree
    # with or even know this value for it to be useful on a ticket, so a
    # local/Free config's tickets can show the real branch instead of
    # "unknown" even though `branch` itself stays None for those.
    display_branch: str | None = None

    # The EFFECTIVE per-run new-ticket cap, already resolved by the CLI
    # (cli/scan_runner/__init__.py's run_scan_cycle()) from whichever of
    # --ticket-cap (a one-off override, never persisted) or the config's
    # own stored `ticket_cap` (migrations/versions/0008_add_ticket_cap.py)
    # applies - None here means neither was set, and
    # create_tickets_activity falls back to its own BACKLOG_CAP constant.
    # This model never re-resolves the two itself; by the time it's built,
    # that decision has already been made once, client-side.
    ticket_cap: int | None = None
