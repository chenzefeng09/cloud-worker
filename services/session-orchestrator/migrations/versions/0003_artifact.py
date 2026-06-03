"""Add kind + run_id columns to attachment for artifact support.

Revision ID: 0003_artifact
Revises: 0002_attachment
Create Date: 2026-06-03

Notes
-----
``kind`` distinguishes user-uploaded inputs (default 'input') from agent-produced
artifacts ('output').  ``run_id`` links output attachments back to the run that
produced them.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_artifact"
down_revision: str | None = "0002_attachment"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "attachment",
        sa.Column("kind", sa.String(10), nullable=False, server_default="input"),
        schema="session_orchestrator",
    )
    op.add_column(
        "attachment",
        sa.Column("run_id", sa.String(30), nullable=True),
        schema="session_orchestrator",
    )
    op.create_index(
        "ix_attachment_kind",
        "attachment",
        ["kind"],
        schema="session_orchestrator",
    )


def downgrade() -> None:
    op.drop_index("ix_attachment_kind", table_name="attachment", schema="session_orchestrator")
    op.drop_column("attachment", "run_id", schema="session_orchestrator")
    op.drop_column("attachment", "kind", schema="session_orchestrator")
