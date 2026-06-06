"""Create user_installed_skills table with per-tenant RLS.

Tracks which skills a user has installed (the middle layer between
Skill.visibility org-publish and per-session enablement). ``enabled_by_default``
lives here so two users can install the same skill with different defaults.

Revision ID: 0002_user_installed_skill
Revises: 0001_init
Create Date: 2026-06-06
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_user_installed_skill"
down_revision: str | None = "0001_init"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "user_installed_skills",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("tenant_id", sa.String(40), nullable=False, index=True),
        sa.Column("user_id", sa.String(120), nullable=False, index=True),
        sa.Column(
            "skill_id",
            sa.String(40),
            sa.ForeignKey("skills.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "enabled_by_default",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "installed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "tenant_id", "user_id", "skill_id", name="uq_user_installed_skill",
        ),
    )

    # Tenant-scoped RLS, mirroring the skills table policies. Each statement is
    # executed separately — the asyncpg driver cannot run multiple commands in a
    # single prepared statement.
    op.execute("ALTER TABLE user_installed_skills ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE user_installed_skills FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY uis_tenant_select ON user_installed_skills "
        "FOR SELECT TO app_user "
        "USING (tenant_id = current_setting('app.tenant_id', true))"
    )
    op.execute(
        "CREATE POLICY uis_tenant_insert ON user_installed_skills "
        "FOR INSERT TO app_user "
        "WITH CHECK (tenant_id = current_setting('app.tenant_id', true))"
    )
    op.execute(
        "CREATE POLICY uis_tenant_update ON user_installed_skills "
        "FOR UPDATE TO app_user "
        "USING (tenant_id = current_setting('app.tenant_id', true)) "
        "WITH CHECK (tenant_id = current_setting('app.tenant_id', true))"
    )
    op.execute(
        "CREATE POLICY uis_tenant_delete ON user_installed_skills "
        "FOR DELETE TO app_user "
        "USING (tenant_id = current_setting('app.tenant_id', true))"
    )

    # The app connects as app_user; grant table DML (RLS still constrains rows).
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON user_installed_skills TO app_user"
    )


def downgrade() -> None:
    op.drop_table("user_installed_skills")
