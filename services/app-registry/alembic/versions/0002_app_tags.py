"""Add tags JSONB column to apps.

Revision ID: 0002_app_tags
Revises: 0001_init
Create Date: 2026-07-21
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0002_app_tags"
down_revision: str = "0001_init"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # app_user already holds SELECT/INSERT/UPDATE/DELETE on ``apps`` (granted in
    # 0001); column-level privileges inherit from the table grant, so a new
    # column is immediately accessible without an extra GRANT.  RLS policies
    # are table-scoped and apply to the new column too - no policy changes
    # needed.  Each statement runs in its own op.execute: asyncpg (the
    # migrator driver) rejects multiple commands in one execute().
    op.add_column(
        "apps",
        sa.Column(
            "tags",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("apps", "tags")
