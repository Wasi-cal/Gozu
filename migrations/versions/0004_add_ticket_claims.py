# Copyright (c) 2026 Calfus Inc.
# Author: Wasiullah Rafeeq S
"""add ticket_claims

Revision ID: 0004_add_ticket_claims
Revises: 0003_add_sonar_plan_and_branches
Create Date: 2026-09-07

Idempotency ledger for ticket creation (ticket/claims.py). Closes a
check-then-act race in create_tickets_activity: without this, two
overlapping runs (concurrent multi-branch fan-out hitting the same
underlying project, an activity retry after a partial failure, or two
configs pointed at the same SonarQube + Jira project) could each search
Jira, each see no existing ticket, and each create one - a duplicate.
`destination` scopes this per ticket backend + project (e.g.
"jira:https://x.atlassian.net:PROJ", see TicketClient.destination_id()),
so two genuinely different destinations tracking the same finding_key
don't collide with each other.

A row with ticket_key still NULL means a claim is in progress; ticket/
claims.py's claim() self-heals a crashed attempt by freeing a claim
that's been NULL for too long, rather than blocking that finding forever.

This revision (and every one touching ticket_claims after it) is
DDL-only, same as every other migration here - ticket/claims.py's own
claim()/release()/record_ticket()/get_ticket() semantics are untouched
by this migration system entirely, and must stay that way.
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0004_add_ticket_claims"
down_revision: Union[str, None] = "0003_add_sonar_plan_and_branches"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE ticket_claims (
            destination  TEXT NOT NULL,
            finding_key  TEXT NOT NULL,
            ticket_key   TEXT,
            created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (destination, finding_key)
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE ticket_claims")
