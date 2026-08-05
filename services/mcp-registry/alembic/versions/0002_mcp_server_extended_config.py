"""Extend mcp_servers with headers/auth/timeout/tool_filter/args.

Revision ID: 0002_mcp_server_extended_config
Revises: 0001_init
Create Date: 2026-07-20
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0002_mcp_server_extended_config"
down_revision: str | None = "0001_init"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Column-level changes only.  The existing row-level policies from 0001
# (mcp_servers_tenant_{select,insert,update,delete}) are row-scoped, not
# column-scoped, so they continue to govern the new columns unchanged.  The
# ``GRANT SELECT, INSERT, UPDATE, DELETE ON mcp_servers TO app_user`` from 0001
# is a table-level grant and covers future columns automatically - no
# additional GRANT needed here.  Each ``op.add_column`` is a single statement
# so asyncpg (the migrator driver, which rejects multi-command execute) is
# happy without manual splitting.


def upgrade() -> None:
    op.add_column(
        "mcp_servers",
        sa.Column(
            "headers",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.add_column(
        "mcp_servers",
        sa.Column("auth", postgresql.JSONB, nullable=True),
    )
    op.add_column(
        "mcp_servers",
        sa.Column("timeout_seconds", sa.Integer, nullable=True),
    )
    op.add_column(
        "mcp_servers",
        sa.Column(
            "tool_filter",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.add_column(
        "mcp_servers",
        sa.Column(
            "args",
            postgresql.ARRAY(sa.Text),
            nullable=False,
            server_default=sa.text("ARRAY[]::text[]"),
        ),
    )


def downgrade() -> None:
    op.drop_column("mcp_servers", "args")
    op.drop_column("mcp_servers", "tool_filter")
    op.drop_column("mcp_servers", "timeout_seconds")
    op.drop_column("mcp_servers", "auth")
    op.drop_column("mcp_servers", "headers")
