"""Add documents.origin_user_id for per-user memory isolation.

Revision ID: 0004_add_origin_user_id
Revises: 0003_add_origin_session_id
Create Date: 2026-07-18

Records the user that owns a run-completion memory document so that
``GET /search`` only returns a given user's own memory docs (plus tenant-shared
ordinary uploads, whose ``origin_session_id`` is NULL).  Without this column the
service filtered by ``tenant_id`` alone, letting any user in a tenant retrieve
another user's session memory.  NULL for ordinary uploads (they stay
tenant-shared).  Existing memory docs have NULL here until back-filled from the
originating session's owner; while NULL they are excluded from search (a
deliberate fail-closed choice).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0004_add_origin_user_id"
down_revision: str | None = "0003_add_origin_session_id"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "documents",
        sa.Column("origin_user_id", sa.String(64), nullable=True),
    )
    op.create_index(
        "ix_documents_origin_user_id",
        "documents",
        ["origin_user_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_documents_origin_user_id", table_name="documents")
    op.drop_column("documents", "origin_user_id")
