# Copyright (c) 2026 Calfus Inc.
# Author: Wasiullah Rafeeq S
"""add configs.sonar_plan, configs.branches

Revision ID: 0003_add_sonar_plan_and_branches
Revises: 0002_add_project_key
Create Date: 2026-09-07

Same reasoning as project_key: nullable so a pre-existing config isn't
broken, and treated as "no restriction" wherever it's read
(config/store.py, receiver/app.py, cli/scan_runner/) rather than an
error - not every scanner_mode has a "plan" concept (Local configs leave
sonar_plan null), and not every config tracks more than one branch (a
null/empty branches value means "any branch", not "no branches").
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0003_add_sonar_plan_and_branches"
down_revision: Union[str, None] = "0002_add_project_key"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE configs ADD COLUMN sonar_plan TEXT")
    op.execute("ALTER TABLE configs ADD COLUMN branches TEXT")


def downgrade() -> None:
    # Reverse order of upgrade().
    op.execute("ALTER TABLE configs DROP COLUMN branches")
    op.execute("ALTER TABLE configs DROP COLUMN sonar_plan")
