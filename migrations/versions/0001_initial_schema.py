# Copyright (c) 2026 Calfus Inc.
# Author: Wasiullah Rafeeq S
"""initial schema: configs, config_credentials

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-09-07

A "config" is one named set of scanner + ticket-backend credentials.
Called "configs", not "profiles" - Docker Compose already has an
unrelated concept called profiles (for conditionally starting services),
and reusing that word here throughout the codebase/docs would be
confusing.

Deliberately normalized: `configs` only holds the fields every backend
has in common (which scanner, local/cloud, which ticket backend, how
it's triggered). Anything backend-specific (a Sonar host URL, a Jira
project key, whatever a future scanner/ticket backend needs) lives in
config_credentials as arbitrary key/value pairs instead of being baked
in as dedicated columns - adding a new scanner or ticket backend never
requires a schema change here, matching how ScannerClient/TicketClient
are themselves generic over the backend.
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0001_initial_schema"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE configs (
            id             SERIAL PRIMARY KEY,
            name           TEXT NOT NULL UNIQUE,
            scanner_type   TEXT NOT NULL,               -- e.g. "sonarqube"
            scanner_mode   TEXT NOT NULL,                -- "local" or "cloud"
            ticket_backend TEXT NOT NULL DEFAULT 'jira',
            trigger_mode   TEXT NOT NULL,                -- "direct", "webhook", "watch", "github_poll", ...
            created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    # Every credential/setting value is Fernet-encrypted ciphertext (see
    # config/crypto.py), never plaintext - key names are backend-specific
    # strings (sonar_host_url, sonar_token, sonar_organization, jira_url,
    # jira_email, jira_api_token, jira_project_key, webhook_secret, ...),
    # not fixed columns.
    op.execute(
        """
        CREATE TABLE config_credentials (
            id         SERIAL PRIMARY KEY,
            config_id  INTEGER NOT NULL REFERENCES configs(id) ON DELETE CASCADE,
            key        TEXT NOT NULL,
            value      TEXT NOT NULL,
            UNIQUE (config_id, key)
        )
        """
    )


def downgrade() -> None:
    # Children before parents - config_credentials.config_id references configs.
    op.execute("DROP TABLE config_credentials")
    op.execute("DROP TABLE configs")
