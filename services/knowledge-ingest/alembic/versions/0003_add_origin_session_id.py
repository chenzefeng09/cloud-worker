"""Add documents.origin_session_id for same-session memory exclusion.

Revision ID: 0003_add_origin_session_id
Revises: 0002_enable_pgvector
Create Date: 2026-07-10

Records the session that produced a run-completion memory document so that
``GET /search?exclude_origin_session_id=<sid>`` can drop a session's own prior
Q&A memory docs.  Those turns are already fed back into the agent via the
restored conversation history (worker ``task["history"]``); re-feeding them as
RAG on top of that double-injects the same context.  NULL for ordinary uploads.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0003_add_origin_session_id"
down_revision: str | None = "0002_enable_pgvector"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "documents",
        sa.Column("origin_session_id", sa.String(64), nullable=True),
    )
    op.create_index(
        "ix_documents_origin_session_id",
        "documents",
        ["origin_session_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_documents_origin_session_id", table_name="documents")
    op.drop_column("documents", "origin_session_id")
