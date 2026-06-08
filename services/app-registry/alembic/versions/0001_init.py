"""Create apps + app_versions tables with per-tenant RLS on both.

Revision ID: 0001_init
Revises:
Create Date: 2026-06-08
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
        "apps",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("tenant_id", sa.String(40), nullable=False, index=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("description", sa.String(2000), nullable=True),
        sa.Column("icon", sa.String(120), nullable=True),
        sa.Column("author_id", sa.String(120), nullable=False, index=True),
        sa.Column(
            "visibility",
            sa.String(20),
            nullable=False,
            server_default="private",
        ),
        sa.Column("current_version_id", sa.String(40), nullable=True),
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
        sa.UniqueConstraint("tenant_id", "name", name="uq_app_tenant_name"),
    )

    op.create_table(
        "app_versions",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column(
            "app_id",
            sa.String(40),
            sa.ForeignKey("apps.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("tenant_id", sa.String(40), nullable=False, index=True),
        sa.Column("version", sa.String(40), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="draft"),
        sa.Column("system_prompt", sa.Text, nullable=False),
        sa.Column("user_prompt_template", sa.Text, nullable=False),
        sa.Column("model", sa.String(120), nullable=False),
        sa.Column("reasoning_effort", sa.String(20), nullable=True),
        sa.Column(
            "sampling_params",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "forced_skills",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "forced_mcp_server_ids",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "input_schema",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("submitted_by", sa.String(120), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reviewed_by", sa.String(120), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("review_note", sa.String(2000), nullable=True),
        sa.Column("created_by", sa.String(120), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("app_id", "version", name="uq_app_version"),
    )

    # Grant the runtime role schema USAGE + table DML.  app_migrator owns the
    # app_registry schema; app_user (non-owner) needs these to reach the tables.
    op.execute("GRANT USAGE ON SCHEMA app_registry TO app_user")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON apps TO app_user")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON app_versions TO app_user")

    # RLS on BOTH tables (improvement over skill-store): app_versions carries
    # its own tenant_id, so the cross-app review queue stays tenant-scoped at
    # the DB layer too.  Each statement runs in its own op.execute: asyncpg
    # (the migrator driver) uses prepared statements and rejects multiple
    # commands in one execute().
    op.execute("ALTER TABLE apps ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE apps FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY apps_tenant_select ON apps"
        " FOR SELECT TO app_user"
        " USING (tenant_id = current_setting('app.tenant_id', true))"
    )
    op.execute(
        "CREATE POLICY apps_tenant_insert ON apps"
        " FOR INSERT TO app_user"
        " WITH CHECK (tenant_id = current_setting('app.tenant_id', true))"
    )
    op.execute(
        "CREATE POLICY apps_tenant_update ON apps"
        " FOR UPDATE TO app_user"
        " USING (tenant_id = current_setting('app.tenant_id', true))"
        " WITH CHECK (tenant_id = current_setting('app.tenant_id', true))"
    )
    op.execute(
        "CREATE POLICY apps_tenant_delete ON apps"
        " FOR DELETE TO app_user"
        " USING (tenant_id = current_setting('app.tenant_id', true))"
    )

    op.execute("ALTER TABLE app_versions ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app_versions FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY app_versions_tenant_select ON app_versions"
        " FOR SELECT TO app_user"
        " USING (tenant_id = current_setting('app.tenant_id', true))"
    )
    op.execute(
        "CREATE POLICY app_versions_tenant_insert ON app_versions"
        " FOR INSERT TO app_user"
        " WITH CHECK (tenant_id = current_setting('app.tenant_id', true))"
    )
    op.execute(
        "CREATE POLICY app_versions_tenant_update ON app_versions"
        " FOR UPDATE TO app_user"
        " USING (tenant_id = current_setting('app.tenant_id', true))"
        " WITH CHECK (tenant_id = current_setting('app.tenant_id', true))"
    )
    op.execute(
        "CREATE POLICY app_versions_tenant_delete ON app_versions"
        " FOR DELETE TO app_user"
        " USING (tenant_id = current_setting('app.tenant_id', true))"
    )


def downgrade() -> None:
    op.drop_table("app_versions")
    op.drop_table("apps")
