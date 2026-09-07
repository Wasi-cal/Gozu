# Copyright (c) 2026 Calfus Inc.
# Author: Wasiullah Rafeeq S
"""add ticket_claims.status

Revision ID: 0006_add_ticket_claims_status
Revises: 0005_add_ticket_destinations
Create Date: 2026-09-07

Whether a claim's ticket has since been auto-closed (see
ticket/claims.py's list_open()/mark_closed(), and
temporal/activities/reconcile_resolved_findings.py, which checks every
'open' claim against the scanner each run and flips one to 'closed' once
SonarQube reports the underlying finding resolved). Every existing row
defaults to 'open' - nothing already in this ledger should retroactively
look closed the moment this revision runs; it only ever moves
open -> closed going forward, from real scanner state.
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0006_add_ticket_claims_status"
down_revision: Union[str, None] = "0005_add_ticket_destinations"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE ticket_claims ADD COLUMN status TEXT NOT NULL DEFAULT 'open'")


def downgrade() -> None:
    op.execute("ALTER TABLE ticket_claims DROP COLUMN status")
