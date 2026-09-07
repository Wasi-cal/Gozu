# Copyright (c) 2026 Calfus Inc.
# Author: Wasiullah Rafeeq S
"""add ticket_destinations, ticket_destination_credentials, configs.ticket_destination_id

Revision ID: 0005_add_ticket_destinations
Revises: 0004_add_ticket_claims
Create Date: 2026-09-07

A ticket destination is one shared board + credentials (today: a Jira
project + its URL/email/API token) that more than one config can point
at, instead of each config carrying its own copy of the same
credentials. Purely additive, same philosophy as project_key/sonar_plan/
branches: `configs.ticket_destination_id` is nullable and left NULL on
every config that already exists - config/store.py's get_config() falls
back to reading Jira fields straight out of config_credentials (the
original, still-supported shape) whenever it's NULL, and only resolves
them from these tables when it's set. No backfill - a pre-existing
config's embedded credentials are left exactly as they are.
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0005_add_ticket_destinations"
down_revision: Union[str, None] = "0004_add_ticket_claims"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE ticket_destinations (
            id             SERIAL PRIMARY KEY,
            name           TEXT NOT NULL UNIQUE,
            ticket_backend TEXT NOT NULL DEFAULT 'jira',
            project_key    TEXT NOT NULL,  -- the Jira project key, not a secret
            created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    # Same shape as config_credentials: Fernet-encrypted arbitrary
    # key/value pairs (jira_url, jira_email, jira_api_token), not fixed
    # columns.
    op.execute(
        """
        CREATE TABLE ticket_destination_credentials (
            id             SERIAL PRIMARY KEY,
            destination_id INTEGER NOT NULL REFERENCES ticket_destinations(id) ON DELETE CASCADE,
            key            TEXT NOT NULL,
            value          TEXT NOT NULL,
            UNIQUE (destination_id, key)
        )
        """
    )
    op.execute(
        "ALTER TABLE configs ADD COLUMN ticket_destination_id INTEGER REFERENCES ticket_destinations(id)"
    )


def downgrade() -> None:
    # Reverse order of upgrade(): the FK column referencing
    # ticket_destinations first, then the tables, children before parents.
    op.execute("ALTER TABLE configs DROP COLUMN ticket_destination_id")
    op.execute("DROP TABLE ticket_destination_credentials")
    op.execute("DROP TABLE ticket_destinations")
