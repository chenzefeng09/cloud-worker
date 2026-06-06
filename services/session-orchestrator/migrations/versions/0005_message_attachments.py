"""Add attachments JSON column to message for attachment-message association.

Revision ID: 0005_message_attachments
Revises: 0004_run_tokens
Create Date: 2026-06-05

Notes
-----
Stores a JSON array of attachment metadata directly on the message row so
that user messages can carry [{id, filename, mime, size}] without a separate
join table.  Nullable — existing rows carry NULL which the schema maps to [].
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005_message_attachments"
down_revision: str | None = "0004_run_tokens"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "message",
        sa.Column(
            "attachments",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        schema="session_orchestrator",
    )


def downgrade() -> None:
    op.drop_column("message", "attachments", schema="session_orchestrator")
