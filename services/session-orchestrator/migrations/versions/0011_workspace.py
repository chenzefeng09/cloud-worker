"""Workspace — reusable instruction/skill/file bundles a session rides on.

Revision ID: 0011_workspace
Revises: 0010_run_execution_mode
Create Date: 2026-07-16

Notes
-----
Two new tables plus a ``session.workspace_id`` column.

* ``workspace`` — a user-owned bundle (id ``wsp_…``) of instructions, skills,
  and files, scoped to a tenant + user with the same RLS discipline as
  ``session``.  At most one ``is_default`` workspace per user, enforced by a
  partial unique index (``tenant_id, user_id WHERE is_default AND deleted_at IS
  NULL``).  Soft-deletes via ``deleted_at``.
* ``workspace_file`` — a file attached to a workspace (id ``wsf_…``); raw bytes
  live in S3 under ``blob_key``, this row is the metadata record.

Same RLS discipline as 0001_init / 0009_canvas: enable + FORCE RLS, four
explicit per-command policies (SELECT / INSERT / UPDATE / DELETE, never
FOR ALL) scoped ``TO app_user`` on the ``tenant_id = current_setting(
'app.tenant_id', true)`` predicate.

asyncpg (the runtime driver) uses prepared statements and rejects multiple SQL
commands in a single ``execute()`` — so every GRANT / ALTER / CREATE POLICY is
its own ``op.execute`` call.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011_workspace"
down_revision: str | None = "0010_run_execution_mode"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _enable_rls(table: str) -> None:
    """Enable + force RLS and install the four per-command tenant policies.

    Each statement is a separate ``op.execute`` (asyncpg forbids multiple
    commands per execute).  Policy predicate mirrors 0009_canvas exactly.
    """
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
    # ── workspace ────────────────────────────────────────────────────────────
    op.create_table(
        "workspace",
        sa.Column("id", sa.String(30), primary_key=True),
        sa.Column("tenant_id", sa.String(30), nullable=False, index=True),
        sa.Column("user_id", sa.String(30), nullable=False, index=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.String(500), nullable=True),
        sa.Column("instructions", sa.Text, nullable=True),
        sa.Column("skills", postgresql.JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("is_default", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("meta", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
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
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        schema="session_orchestrator",
    )
    _enable_rls("workspace")

    # At most one default workspace per (tenant, user) among live rows.
    op.execute(
        "CREATE UNIQUE INDEX uq_workspace_default_per_user"
        " ON session_orchestrator.workspace (tenant_id, user_id)"
        " WHERE is_default AND deleted_at IS NULL"
    )

    # ── workspace_file ───────────────────────────────────────────────────────
    op.create_table(
        "workspace_file",
        sa.Column("id", sa.String(30), primary_key=True),
        sa.Column("tenant_id", sa.String(30), nullable=False, index=True),
        sa.Column(
            "workspace_id",
            sa.String(30),
            sa.ForeignKey("session_orchestrator.workspace.id"),
            nullable=False,
            index=True,
        ),
        sa.Column("user_id", sa.String(30), nullable=False),
        sa.Column("filename", sa.String(500), nullable=False),
        sa.Column(
            "mime",
            sa.String(200),
            nullable=False,
            server_default="application/octet-stream",
        ),
        sa.Column("size_bytes", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("blob_key", sa.Text, nullable=False),
        sa.Column("description", sa.String(500), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        schema="session_orchestrator",
    )
    _enable_rls("workspace_file")

    # ── session.workspace_id ─────────────────────────────────────────────────
    # Soft reference (no FK) to the workspace the session was created against;
    # NULL for sessions not scoped to a workspace.  No backfill.
    op.add_column(
        "session",
        sa.Column("workspace_id", sa.String(30), nullable=True),
        schema="session_orchestrator",
    )
    op.create_index(
        "ix_session_orchestrator_session_workspace_id",
        "session",
        ["workspace_id"],
        schema="session_orchestrator",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_session_orchestrator_session_workspace_id",
        table_name="session",
        schema="session_orchestrator",
    )
    op.drop_column("session", "workspace_id", schema="session_orchestrator")
    # CASCADE drops the policies with the table; child first to respect the FK
    # ordering (workspace_file → workspace).
    op.execute("DROP TABLE IF EXISTS session_orchestrator.workspace_file CASCADE")
    op.execute("DROP TABLE IF EXISTS session_orchestrator.workspace CASCADE")
