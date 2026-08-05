"""Persist discovered MCP tools on mcp_servers.

Revision ID: 0004_mcp_server_discovered_tools
Revises: 0003_drop_mcp_server_scopes
Create Date: 2026-07-20

Adds ``mcp_servers.discovered_tools`` (JSONB on Postgres, JSON on SQLite),
a NOT NULL column defaulting to ``'[]'``.  The ``/mcp-servers/{id}/test``
endpoint writes the upstream's ``tools/list`` result here on every successful
handshake, so the admin console list page can render the tool catalog from
the DB without re-probing each server on every page load.  The id-less
``/probe`` endpoint stays a pure read and never writes.

Each item is ``{"name": str, "description": str | None}`` - the shape of
``McpTestTool`` in ``schemas/mcp_server.py``.  Extra fields the upstream
returns (e.g. ``inputSchema``) are dropped by the tester before persistence,
keeping the stored shape stable across MCP protocol revisions.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0004_mcp_server_discovered_tools"
down_revision: str | None = "0003_drop_mcp_server_scopes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Column-level change only.  The row-level policies from 0001
# (mcp_servers_tenant_{select,insert,update,delete}) are row-scoped, not
# column-scoped, so they govern the new column unchanged.  The 0001 GRANT is
# table-level and covers future columns automatically.  Each ``op.add_column``
# is a single statement, which keeps asyncpg (the migrator driver, which
# rejects multi-command execute) happy.


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        column = sa.Column(
            "discovered_tools",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        )
    else:
        # SQLite (unit-test path via Base.metadata.create_all does not run
        # alembic, but we keep the branch for completeness / manual runs).
        column = sa.Column(
            "discovered_tools",
            sa.JSON,
            nullable=False,
            server_default=sa.text("'[]'"),
        )
    op.add_column("mcp_servers", column)


def downgrade() -> None:
    op.drop_column("mcp_servers", "discovered_tools")
