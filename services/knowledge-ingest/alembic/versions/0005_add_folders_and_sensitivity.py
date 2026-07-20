"""Add folders table + documents.folder_id/sensitivity/uploaded_by_user_id.

Revision ID: 0005_add_folders_and_sensitivity
Revises: 0004_add_origin_user_id
Create Date: 2026-07-19

Knowledge-library v1: a tenant-scoped folder tree plus a per-document
sensitivity (L1..L5) for the visibility gate, and a reserved uploaded_by_user_id
for a future personal knowledge source.

Back-fill: the sensitivity column is added NOT NULL with a server_default of
``L1``, which fills every existing row (ordinary uploads and memory docs alike)
with L1 — satisfying "存量非 memory 文档 → sensitivity='L1'".  folder_id and
uploaded_by_user_id are nullable, so their back-fill value is simply NULL.

asyncpg (the migrator driver) uses prepared statements and rejects multiple
commands in one execute(), so every raw GRANT / RLS statement runs in its own
op.execute().
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0005_add_folders_and_sensitivity"
down_revision: str | None = "0004_add_origin_user_id"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ── folders table ───────────────────────────────────────────────────
    op.create_table(
        "folders",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("tenant_id", sa.String(40), nullable=False, index=True),
        sa.Column("parent_id", sa.String(40), nullable=True, index=True),
        sa.Column("name", sa.String(500), nullable=False),
        sa.Column(
            "default_sensitivity", sa.String(8), nullable=False, server_default="L1",
        ),
        sa.Column("created_by_user_id", sa.String(64), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["parent_id"], ["folders.id"],
            name="fk_folders_parent_id",
            ondelete="RESTRICT",
        ),
    )

    # Runtime role needs schema-scoped DML on the new table (owner is
    # app_migrator; app_user reaches it under RLS).
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON folders TO app_user")

    op.execute("ALTER TABLE folders ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE folders FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY folders_tenant_select ON folders"
        " FOR SELECT TO app_user"
        " USING (tenant_id = current_setting('app.tenant_id', true))"
    )
    op.execute(
        "CREATE POLICY folders_tenant_insert ON folders"
        " FOR INSERT TO app_user"
        " WITH CHECK (tenant_id = current_setting('app.tenant_id', true))"
    )
    op.execute(
        "CREATE POLICY folders_tenant_update ON folders"
        " FOR UPDATE TO app_user"
        " USING (tenant_id = current_setting('app.tenant_id', true))"
        " WITH CHECK (tenant_id = current_setting('app.tenant_id', true))"
    )
    op.execute(
        "CREATE POLICY folders_tenant_delete ON folders"
        " FOR DELETE TO app_user"
        " USING (tenant_id = current_setting('app.tenant_id', true))"
    )

    # ── documents new columns ───────────────────────────────────────────
    op.add_column("documents", sa.Column("folder_id", sa.String(40), nullable=True))
    op.create_foreign_key(
        "fk_documents_folder_id",
        "documents",
        "folders",
        ["folder_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_documents_folder_id", "documents", ["folder_id"])

    # NOT NULL + server_default back-fills every existing row with L1.
    op.add_column(
        "documents",
        sa.Column("sensitivity", sa.String(8), nullable=False, server_default="L1"),
    )

    op.add_column(
        "documents", sa.Column("uploaded_by_user_id", sa.String(64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("documents", "uploaded_by_user_id")
    op.drop_column("documents", "sensitivity")
    op.drop_index("ix_documents_folder_id", table_name="documents")
    op.drop_constraint("fk_documents_folder_id", "documents", type_="foreignkey")
    op.drop_column("documents", "folder_id")

    op.execute("DROP POLICY IF EXISTS folders_tenant_delete ON folders")
    op.execute("DROP POLICY IF EXISTS folders_tenant_update ON folders")
    op.execute("DROP POLICY IF EXISTS folders_tenant_insert ON folders")
    op.execute("DROP POLICY IF EXISTS folders_tenant_select ON folders")
    op.drop_table("folders")
