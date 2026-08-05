"""Workspace sensitivity_level - per-workspace and per-file clearance tier.

Revision ID: 0012_workspace_sensitivity
Revises: 0011_workspace
Create Date: 2026-07-18

Notes
-----
Adds a ``sensitivity_level`` column (default ``L2``) to both ``workspace`` and
``workspace_file``.  Levels are L1..L5 (L1 < L2 < L3 < L4 < L5) and the server
is authoritative: ``file_level <= workspace_level <= user_clearance``.  A file's
level may not exceed its workspace's level (422) and a workspace's level may not
be lowered below any existing file's level (409); both are enforced in
``workspace_service.py``.

No new RLS policies - the columns ride the existing per-tenant policies
installed by 0011_workspace.  asyncpg forbids multiple SQL commands per
``execute()``, so each DDL statement is its own ``op.add_column`` call (mirrors
the ``session.workspace_id`` add in 0011).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012_workspace_sensitivity"
down_revision: str | None = "0011_workspace"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "workspace",
        sa.Column(
            "sensitivity_level",
            sa.String(8),
            nullable=False,
            server_default="L2",
        ),
        schema="session_orchestrator",
    )
    op.add_column(
        "workspace_file",
        sa.Column(
            "sensitivity_level",
            sa.String(8),
            nullable=False,
            server_default="L2",
        ),
        schema="session_orchestrator",
    )


def downgrade() -> None:
    op.drop_column("workspace_file", "sensitivity_level", schema="session_orchestrator")
    op.drop_column("workspace", "sensitivity_level", schema="session_orchestrator")
