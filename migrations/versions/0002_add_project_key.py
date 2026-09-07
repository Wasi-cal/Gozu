# Copyright (c) 2026 Calfus Inc.
# Author: Wasiullah Rafeeq S
"""add configs.project_key

Revision ID: 0002_add_project_key
Revises: 0001_initial_schema
Create Date: 2026-09-07

Nullable at the DB level so a pre-existing config isn't broken by a
NOT NULL constraint; the CLI wizard still treats it as a required prompt
for any new config (see cli/init_wizard/) - `gozu run` needs it (no
webhook payload to pull it from for direct invocation), so a config
created before this revision must be recreated via `gozu init` before it
can be used with `gozu run`.
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0002_add_project_key"
down_revision: Union[str, None] = "0001_initial_schema"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE configs ADD COLUMN project_key TEXT")


def downgrade() -> None:
    op.execute("ALTER TABLE configs DROP COLUMN project_key")
