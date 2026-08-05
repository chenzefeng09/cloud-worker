"""Drop the write-only top-level mcp_servers.scopes column.

Revision ID: 0003_drop_mcp_server_scopes
Revises: 0002_mcp_server_extended_config
Create Date: 2026-07-20

``mcp_servers.scopes`` was written by the API and echoed back on read, but no
consumer ever read it: the SDK worker, the agentscope ``mcp_tenant_injector``,
the orchestrator's ``inline_mcp_client`` and the tool-broker all ignore it.
(The surviving ``auth.scopes`` inside the JSONB ``auth`` column is a different
thing entirely — the OAuth client-credentials ``scope`` form parameter — and is
untouched by this migration.)  Dropping the dead column keeps the write schema
honest now that the admin console models per-tool allow/block via
``tool_filter`` instead.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0003_drop_mcp_server_scopes"
down_revision: str | None = "0002_mcp_server_extended_config"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Column-level change only.  The row-level policies from 0001
# (mcp_servers_tenant_{select,insert,update,delete}) are row-scoped and the
# 0001 GRANT is table-level, so neither needs touching.  Each op is a single
# statement, which keeps asyncpg (the migrator driver, which rejects
# multi-command execute) happy.


def upgrade() -> None:
    op.drop_column("mcp_servers", "scopes")


def downgrade() -> None:
    # Re-create the column exactly as 0001_init defined it.  ``server_default``
    # is mandatory here: the column is NOT NULL and existing rows would
    # otherwise fail the constraint on rollback.  Postgres gets the original
    # ``TEXT[]``; any other dialect (SQLite in tests) gets the JSON list shape
    # the ORM binds there.
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        column = sa.Column(
            "scopes",
            postgresql.ARRAY(sa.Text),
            nullable=False,
            server_default=sa.text("ARRAY[]::text[]"),
        )
    else:
        column = sa.Column(
            "scopes",
            sa.JSON,
            nullable=False,
            server_default=sa.text("'[]'"),
        )
    op.add_column("mcp_servers", column)
