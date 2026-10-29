"""Default new cron jobs to UTC instead of Asia/Shanghai.

Only the column's server default changes; existing rows keep whatever
timezone they were created with.

Revision ID: 0003_timezone_default_utc
Revises: 0002_trigger_columns
Create Date: 2026-10-29
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0003_timezone_default_utc"
down_revision: str | None = "0002_trigger_columns"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("cron_jobs", "timezone", server_default="UTC")


def downgrade() -> None:
    op.alter_column("cron_jobs", "timezone", server_default="Asia/Shanghai")
