"""Create mcp_servers table with per-tenant RLS.

Revision ID: 0001_init
Revises:
Create Date: 2026-06-02
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0001_init"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "mcp_servers",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("tenant_id", sa.String(40), nullable=False, index=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("transport", sa.String(20), nullable=False),
        sa.Column("endpoint", sa.String(500), nullable=True),
        sa.Column("command", sa.String(500), nullable=True),
        sa.Column("env", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column(
            "scopes",
            postgresql.ARRAY(sa.Text),
            nullable=False,
            server_default=sa.text("ARRAY[]::text[]"),
        ),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("tenant_id", "name", name="uq_mcp_tenant_name"),
    )

    # Grant the runtime role access to the schema + table.  app_migrator owns
    # the schema; app_user (the non-owner runtime role) needs USAGE + DML.
    # Tenant isolation is enforced by the RLS policies below.
    op.execute("GRANT USAGE ON SCHEMA mcp_registry TO app_user")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON mcp_servers TO app_user")

    # Each statement runs in its own op.execute: asyncpg (the migrator driver)
    # uses prepared statements and rejects multiple commands in one execute().
    op.execute("ALTER TABLE mcp_servers ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE mcp_servers FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY mcp_servers_tenant_select ON mcp_servers"
        " FOR SELECT TO app_user"
        " USING (tenant_id = current_setting('app.tenant_id', true))"
    )
    op.execute(
        "CREATE POLICY mcp_servers_tenant_insert ON mcp_servers"
        " FOR INSERT TO app_user"
        " WITH CHECK (tenant_id = current_setting('app.tenant_id', true))"
    )
    op.execute(
        "CREATE POLICY mcp_servers_tenant_update ON mcp_servers"
        " FOR UPDATE TO app_user"
        " USING (tenant_id = current_setting('app.tenant_id', true))"
        " WITH CHECK (tenant_id = current_setting('app.tenant_id', true))"
    )
    op.execute(
        "CREATE POLICY mcp_servers_tenant_delete ON mcp_servers"
        " FOR DELETE TO app_user"
        " USING (tenant_id = current_setting('app.tenant_id', true))"
    )


def downgrade() -> None:
    op.drop_table("mcp_servers")
