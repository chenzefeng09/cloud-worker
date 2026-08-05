"""Workspace file versioning + platform config.

Revision ID: 0015_workspace_file_version
Revises: 0014_run_app_id
Create Date: 2026-08-05

Notes
-----
Three schema changes to support workspace file writeback with version snapshots
and admin-configurable retention/cleanup.

* ``workspace_file_version`` — historical snapshot of a workspace file's bytes.
  Created whenever an agent run writes back a modified file (the previous
  ``blob_key`` is preserved here before the ``workspace_file`` row is updated).
* ``platform_config`` — tenant-scoped key-value store for admin-configurable
  platform settings (retention days, max versions, cleanup schedule).
* ``workspace_file.current_version`` — monotonically increasing version counter;
  NULL for legacy files uploaded before this feature.

Same RLS discipline as 0011_workspace: enable + FORCE, four per-command
policies scoped TO app_user on tenant_id.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0015_workspace_file_version"
down_revision: str | None = "0014_run_app_id"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _enable_rls(table: str) -> None:
    qualified = f"session_orchestrator.{table}"
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {qualified} TO app_user")
    op.execute(f"ALTER TABLE {qualified} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {qualified} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY {table}_tenant_select ON {qualified}"
        " FOR SELECT TO app_user"
        " USING (tenant_id = current_setting('app.tenant_id', true))"
    )
    op.execute(
        f"CREATE POLICY {table}_tenant_insert ON {qualified}"
        " FOR INSERT TO app_user"
        " WITH CHECK (tenant_id = current_setting('app.tenant_id', true))"
    )
    op.execute(
        f"CREATE POLICY {table}_tenant_update ON {qualified}"
        " FOR UPDATE TO app_user"
        " USING (tenant_id = current_setting('app.tenant_id', true))"
        " WITH CHECK (tenant_id = current_setting('app.tenant_id', true))"
    )
    op.execute(
        f"CREATE POLICY {table}_tenant_delete ON {qualified}"
        " FOR DELETE TO app_user"
        " USING (tenant_id = current_setting('app.tenant_id', true))"
    )


def upgrade() -> None:
    # ── workspace_file_version ──────────────────────────────────────────────
    op.create_table(
        "workspace_file_version",
        sa.Column("id", sa.String(30), primary_key=True),
        sa.Column("tenant_id", sa.String(30), nullable=False, index=True),
        sa.Column(
            "workspace_file_id",
            sa.String(30),
            sa.ForeignKey("session_orchestrator.workspace_file.id"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("blob_key", sa.Text, nullable=False),
        sa.Column("size_bytes", sa.BigInteger, nullable=False),
        sa.Column("change_summary", sa.Text, nullable=True),
        sa.Column("created_by", sa.String(60), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        schema="session_orchestrator",
    )
    op.create_index(
        "ix_wfv_file_version",
        "workspace_file_version",
        ["workspace_file_id", "version"],
        unique=True,
        schema="session_orchestrator",
    )
    _enable_rls("workspace_file_version")

    # ── platform_config ─────────────────────────────────────────────────────
    op.create_table(
        "platform_config",
        sa.Column("id", sa.String(30), primary_key=True),
        sa.Column("tenant_id", sa.String(30), nullable=False),
        sa.Column("key", sa.String(200), nullable=False),
        sa.Column("value", postgresql.JSONB, nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("updated_by", sa.String(60), nullable=True),
        schema="session_orchestrator",
    )
    op.create_index(
        "uq_platform_config_tenant_key",
        "platform_config",
        ["tenant_id", "key"],
        unique=True,
        schema="session_orchestrator",
    )
    _enable_rls("platform_config")

    # ── workspace_file.current_version ──────────────────────────────────────
    op.add_column(
        "workspace_file",
        sa.Column("current_version", sa.Integer, nullable=True),
        schema="session_orchestrator",
    )


def downgrade() -> None:
    op.drop_column("workspace_file", "current_version", schema="session_orchestrator")
    op.execute("DROP TABLE IF EXISTS session_orchestrator.platform_config CASCADE")
    op.execute("DROP TABLE IF EXISTS session_orchestrator.workspace_file_version CASCADE")
