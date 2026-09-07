# Copyright (c) 2026 Calfus Inc.
# Author: Wasiullah Rafeeq S
"""add configs.ticket_cap

Revision ID: 0008_add_ticket_cap
Revises: 0007_add_last_scan_tracking
Create Date: 2026-09-07

Per-config override for create_tickets_activity's per-run new-ticket cap
(temporal/activities/create_tickets.py's BACKLOG_CAP, default 30).
Nullable, and deliberately NOT backfilled with an explicit 30 for
existing configs - NULL means "use the fallback constant", so a config
predating this column keeps behaving exactly as it does today, not
because it happens to match the current default value.
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0008_add_ticket_cap"
down_revision: Union[str, None] = "0007_add_last_scan_tracking"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE configs ADD COLUMN ticket_cap INTEGER")


def downgrade() -> None:
    op.execute("ALTER TABLE configs DROP COLUMN ticket_cap")
