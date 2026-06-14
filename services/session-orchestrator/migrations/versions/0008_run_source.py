"""Add source column to run to distinguish user vs scheduled runs.

Revision ID: 0008_run_source
Revises: 0007_hitl_paused_status
Create Date: 2026-06-13

Notes
-----
Adds a non-nullable ``source`` column (String(16), server_default "user") to
the ``run`` table so that cron-runner-triggered runs can be identified as
``"schedule"`` while interactive chat/app runs keep the default ``"user"``.
Existing rows are back-filled to "user" via the server_default.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_run_source"
down_revision: str | None = "0007_hitl_paused_status"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "run",
        sa.Column("source", sa.String(16), nullable=False, server_default="user"),
        schema="session_orchestrator",
    )


def downgrade() -> None:
    op.drop_column("run", "source", schema="session_orchestrator")
