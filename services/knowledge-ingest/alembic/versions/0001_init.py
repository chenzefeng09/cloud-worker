"""Create documents + chunks tables with JSON embedding fallback (no pgvector).

Revision ID: 0001_init
Revises:
Create Date: 2026-06-02

NOTE: pgvector not available in current postgres image.
embedding column uses JSON instead of Vector(1536).
IVFFlat index skipped — re-enable when pgvector is installed.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_init"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


EMBEDDING_DIM = 1536


def upgrade() -> None:
    # pgvector not available — skipping CREATE EXTENSION vector
    # op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "documents",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("tenant_id", sa.String(40), nullable=False, index=True),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("source_uri", sa.String(1000), nullable=True),
        sa.Column("mime", sa.String(80), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="PENDING"),
        sa.Column(
            "uploaded_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    op.create_table(
        "chunks",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column(
            "document_id",
            sa.String(40),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("ord", sa.Integer, nullable=False),
        sa.Column("text", sa.Text, nullable=False),
        # JSON fallback — swap to Vector(EMBEDDING_DIM) once pgvector is installed
        sa.Column("embedding", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    # IVFFlat index skipped — requires pgvector extension
    # op.execute(
    #     "CREATE INDEX IF NOT EXISTS chunks_embedding_cosine_idx "
    #     "ON chunks USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100)"
    # )

    # RLS — documents direct; chunks via join.  The chunk policy uses an
    # EXISTS lookup against documents.tenant_id to keep tenant boundaries
    # intact without storing tenant_id redundantly on every chunk row.
    op.execute(
        """
        ALTER TABLE documents ENABLE ROW LEVEL SECURITY;
        ALTER TABLE documents FORCE ROW LEVEL SECURITY;

        CREATE POLICY documents_tenant_select ON documents
            FOR SELECT TO app_user
            USING (tenant_id = current_setting('app.tenant_id', true));

        CREATE POLICY documents_tenant_insert ON documents
            FOR INSERT TO app_user
            WITH CHECK (tenant_id = current_setting('app.tenant_id', true));

        CREATE POLICY documents_tenant_update ON documents
            FOR UPDATE TO app_user
            USING (tenant_id = current_setting('app.tenant_id', true))
            WITH CHECK (tenant_id = current_setting('app.tenant_id', true));

        CREATE POLICY documents_tenant_delete ON documents
            FOR DELETE TO app_user
            USING (tenant_id = current_setting('app.tenant_id', true));

        ALTER TABLE chunks ENABLE ROW LEVEL SECURITY;
        ALTER TABLE chunks FORCE ROW LEVEL SECURITY;

        CREATE POLICY chunks_tenant_select ON chunks
            FOR SELECT TO app_user
            USING (EXISTS (
                SELECT 1 FROM documents d
                WHERE d.id = chunks.document_id
                  AND d.tenant_id = current_setting('app.tenant_id', true)
            ));

        CREATE POLICY chunks_tenant_insert ON chunks
            FOR INSERT TO app_user
            WITH CHECK (EXISTS (
                SELECT 1 FROM documents d
                WHERE d.id = chunks.document_id
                  AND d.tenant_id = current_setting('app.tenant_id', true)
            ));

        CREATE POLICY chunks_tenant_delete ON chunks
            FOR DELETE TO app_user
            USING (EXISTS (
                SELECT 1 FROM documents d
                WHERE d.id = chunks.document_id
                  AND d.tenant_id = current_setting('app.tenant_id', true)
            ));
        """
    )


def downgrade() -> None:
    op.drop_table("chunks")
    op.drop_table("documents")
