"""Add app_snapshot JSONB column to run for App-run harness snapshots.

Revision ID: 0006_run_app_snapshot
Revises: 0005_message_attachments
Create Date: 2026-06-08

Notes
-----
Stores the immutable harness snapshot for an App run (model / version /
sampling params / resolved inputs / rendered-task preview).  Nullable —
ordinary chat runs carry NULL, which the API maps through unchanged.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006_run_app_snapshot"
down_revision: str | None = "0005_message_attachments"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "run",
        sa.Column(
            "app_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        schema="session_orchestrator",
    )


def downgrade() -> None:
    op.drop_column("run", "app_snapshot", schema="session_orchestrator")
