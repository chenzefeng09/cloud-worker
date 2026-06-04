"""Create skills + skill_versions tables with per-tenant RLS on skills.

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
        "skills",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("tenant_id", sa.String(40), nullable=False, index=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("description", sa.String(2000), nullable=True),
        sa.Column("owner", sa.String(120), nullable=True),
        sa.Column(
            "visibility",
            sa.String(20),
            nullable=False,
            server_default="private",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("tenant_id", "name", name="uq_skill_tenant_name"),
    )

    op.create_table(
        "skill_versions",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column(
            "skill_id",
            sa.String(40),
            sa.ForeignKey("skills.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("version", sa.String(40), nullable=False),
        sa.Column(
            "manifest", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column("blob_url", sa.String(1000), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("uploaded_by", sa.String(120), nullable=True),
        sa.Column(
            "uploaded_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("skill_id", "version", name="uq_skill_version"),
    )

    # Grant the runtime role schema USAGE + table DML.  app_migrator owns the
    # skill_store schema; app_user (non-owner) needs these to reach the tables.
    op.execute("GRANT USAGE ON SCHEMA skill_store TO app_user")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON skills TO app_user")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON skill_versions TO app_user")

    # RLS on skills only (skill_versions join through skill_id; covered at
    # the API layer + we still tenant-scope reads via the join).
    # Each statement runs in its own op.execute: asyncpg (the migrator driver)
    # uses prepared statements and rejects multiple commands in one execute().
    op.execute("ALTER TABLE skills ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE skills FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY skills_tenant_select ON skills"
        " FOR SELECT TO app_user"
        " USING (tenant_id = current_setting('app.tenant_id', true))"
    )
    op.execute(
        "CREATE POLICY skills_tenant_insert ON skills"
        " FOR INSERT TO app_user"
        " WITH CHECK (tenant_id = current_setting('app.tenant_id', true))"
    )
    op.execute(
        "CREATE POLICY skills_tenant_update ON skills"
        " FOR UPDATE TO app_user"
        " USING (tenant_id = current_setting('app.tenant_id', true))"
        " WITH CHECK (tenant_id = current_setting('app.tenant_id', true))"
    )
    op.execute(
        "CREATE POLICY skills_tenant_delete ON skills"
        " FOR DELETE TO app_user"
        " USING (tenant_id = current_setting('app.tenant_id', true))"
    )


def downgrade() -> None:
    op.drop_table("skill_versions")
    op.drop_table("skills")
