"""Generalize cron_jobs into two-type triggers (schedule | webhook).

Adds trigger_type / field_mapping / webhook_secret_hash and makes
cron_expression nullable (webhook rows have no cron). Existing rows are
back-filled to trigger_type='schedule' via the server default, so
``alembic upgrade head`` applies cleanly on a DB that already has schedule rows.

Revision ID: 0002_trigger_columns
Revises: 0001_init
Create Date: 2026-07-22
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0002_trigger_columns"
down_revision: str | None = "0001_init"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "cron_jobs",
        sa.Column(
            "trigger_type",
            sa.String(16),
            nullable=False,
            server_default="schedule",
        ),
    )
    op.add_column(
        "cron_jobs",
        sa.Column("field_mapping", postgresql.JSONB, nullable=True),
    )
    op.add_column(
        "cron_jobs",
        sa.Column("webhook_secret_hash", sa.String(), nullable=True),
    )
    # Webhook triggers have no cron expression.
    op.alter_column(
        "cron_jobs",
        "cron_expression",
        existing_type=sa.String(120),
        nullable=True,
    )


def downgrade() -> None:
    # Only schedule rows can satisfy the restored NOT NULL constraint; any
    # webhook rows would block the alter, but downgrade is a dev-only path.
    op.alter_column(
        "cron_jobs",
        "cron_expression",
        existing_type=sa.String(120),
        nullable=False,
    )
    op.drop_column("cron_jobs", "webhook_secret_hash")
    op.drop_column("cron_jobs", "field_mapping")
    op.drop_column("cron_jobs", "trigger_type")
