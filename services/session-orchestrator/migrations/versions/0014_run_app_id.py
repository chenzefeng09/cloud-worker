"""Add app_id column to run for listing an App's execution history.

Revision ID: 0014_run_app_id
Revises: 0013_artifact_user_indexes
Create Date: 2026-07-20

Notes
-----
Denormalises ``app_snapshot->>'app_id'`` onto a first-class, indexed
``run.app_id`` column so the execution history of an App can be queried
without unpacking the JSONB.  NULL for ordinary chat runs (which carry no
``app_snapshot``) - the column is nullable, and ``list_tenant_runs`` treats
``app_id=None`` as "no filter", preserving the existing tenant-wide behaviour.

Type choice: ``String(40)`` (NOT a native UUID).  ``app_registry.apps.id`` is
``String(40)`` carrying prefixed ids like ``app_<ULID hex>`` (see
``app_registry.domain.models.new_id``).  A native ``UUID`` column would reject
those values - both the backfill (``app_snapshot->>'app_id'`` returns text)
and every subsequent insert (the BFF ``run_app`` resolver threads the path
``app_id`` straight through).  Matching the registry's column type keeps the
foreign-value shape valid end-to-end.

Backfill: existing rows whose ``app_snapshot`` carries an ``app_id`` are
populated in-place; rows with NULL ``app_snapshot`` (chat runs) stay NULL.

RLS / grants: the ``run`` table already has table-level
``GRANT SELECT, INSERT, UPDATE, DELETE TO app_user`` (0001_init) and the four
per-command tenant policies.  A new column on an RLS table rides both the
existing table-level grant and the existing policies - no column-level GRANT
and no new policy are needed (mirrors 0004_run_tokens / 0006_run_app_snapshot /
0008_run_source / 0010_run_execution_mode / 0012_workspace_sensitivity).

asyncpg forbids multiple SQL commands in a single ``execute()`` - each DDL
statement and the backfill UPDATE are separate ``op`` calls.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014_run_app_id"
down_revision: str | None = "0013_artifact_user_indexes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "run",
        sa.Column("app_id", sa.String(40), nullable=True),
        schema="session_orchestrator",
    )
    op.create_index(
        "ix_run_app_id",
        "run",
        ["app_id"],
        schema="session_orchestrator",
    )
    # Backfill from the snapshot so historical App runs are queryable by app
    # immediately.  ``app_snapshot->>'app_id'`` returns NULL when the key is
    # absent, so the IS NOT NULL guard on the JSONB value keeps chat runs NULL.
    op.execute(
        "UPDATE session_orchestrator.run"
        " SET app_id = app_snapshot->>'app_id'"
        " WHERE app_snapshot IS NOT NULL"
        " AND app_snapshot->>'app_id' IS NOT NULL"
    )


def downgrade() -> None:
    op.drop_index(
        "ix_run_app_id",
        table_name="run",
        schema="session_orchestrator",
    )
    op.drop_column("run", "app_id", schema="session_orchestrator")
