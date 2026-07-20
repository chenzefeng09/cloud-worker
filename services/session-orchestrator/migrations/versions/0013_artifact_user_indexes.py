"""Artifact user indexes — index canvas_document(user_id) and attachment(user_id).

Revision ID: 0013_artifact_user_indexes
Revises: 0012_workspace_sensitivity
Create Date: 2026-07-18

Notes
-----
Both ``canvas_document.user_id`` and ``attachment.user_id`` are NOT NULL but
unindexed (see 0009_canvas / 0002_attachment).  The new Artifacts panel lists
every canvas / output file owned by a user *across sessions*, so these two
columns are now on a hot read path filtered + ordered on every panel open.

A tenant-scoped user predicate (``tenant_id + user_id``) is the common shape;
``tenant_id`` is already indexed on both tables, so a standalone index on
``user_id`` is sufficient for the planner — a composite would not improve a
``WHERE tenant_id = ? AND user_id = ?`` plan materially over the two
single-column indexes under RLS, and RLS already rewrites the query to
``current_setting('app.tenant_id')`` which keeps the tenant predicate constant.

asyncpg forbids multiple SQL commands in a single ``execute()`` — each
``op.create_index`` is its own statement (alembic generates one ``CREATE INDEX``
per call), so no extra splitting is needed.  No RLS policy changes: the
indexes are plain B-trees on an existing column.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013_artifact_user_indexes"
down_revision: str | None = "0012_workspace_sensitivity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_canvas_document_user_id",
        "canvas_document",
        ["user_id"],
        schema="session_orchestrator",
    )
    op.create_index(
        "ix_attachment_user_id",
        "attachment",
        ["user_id"],
        schema="session_orchestrator",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_attachment_user_id",
        table_name="attachment",
        schema="session_orchestrator",
    )
    op.drop_index(
        "ix_canvas_document_user_id",
        table_name="canvas_document",
        schema="session_orchestrator",
    )
