"""Input model for ScanToTicketWorkflow."""

from pydantic import BaseModel


class SonarToJiraInput(BaseModel):
    project_key: str
    task_id: str | None = None

    # A specific config's scanner/ticket backend + credentials, as selected
    # by `codescan run` (cli/scan_runner.py) - config_store.get_config()'s
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
