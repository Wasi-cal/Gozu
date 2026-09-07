# Copyright (c) 2026 Calfus Inc.
# Author: Wasiullah Rafeeq S
"""add configs.last_scan_sha, configs.last_scan_dirty

Revision ID: 0007_add_last_scan_tracking
Revises: 0006_add_ticket_claims_status
Create Date: 2026-09-07

Backs `gozu run --skip-unchanged` (cli/scan_runner/__init__.py): the git
HEAD SHA and working-tree-dirty flag as of this config's last
successful scan, so "nothing's changed since last time" can be detected
across separate `gozu run` invocations, not just within one --watch
loop's lifetime - a plain in-memory flag wouldn't survive the process
exiting. Both nullable: every config predates this field, and a config
that never opts into --skip-unchanged simply never has either column
set, which reads identically to "always scan" (see run_scan_cycle()'s
comparison, which treats NULL as "no prior recorded state").
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0007_add_last_scan_tracking"
down_revision: Union[str, None] = "0006_add_ticket_claims_status"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE configs ADD COLUMN last_scan_sha TEXT")
    op.execute("ALTER TABLE configs ADD COLUMN last_scan_dirty BOOLEAN")


def downgrade() -> None:
    # Reverse order of upgrade().
    op.execute("ALTER TABLE configs DROP COLUMN last_scan_dirty")
    op.execute("ALTER TABLE configs DROP COLUMN last_scan_sha")
