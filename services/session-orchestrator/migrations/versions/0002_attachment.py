"""Add attachment table for session file uploads.

Revision ID: 0002_attachment
Revises: 0001_init
Create Date: 2026-06-03

Notes
-----
Same RLS discipline as 0001_init: enable + force RLS, four explicit policies
per table scoped to app_user.  blob_key stores the S3 object key; the actual
bytes live in MinIO under the ``agent-platform`` bucket.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_attachment"
down_revision: str | None = "0001_init"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "attachment",
        sa.Column("id", sa.String(30), primary_key=True),
        sa.Column("tenant_id", sa.String(30), nullable=False, index=True),
        sa.Column(
            "session_id",
            sa.String(30),
            sa.ForeignKey("session_orchestrator.session.id"),
            nullable=False,
            index=True,
        ),
        sa.Column("user_id", sa.String(30), nullable=False),
        sa.Column("filename", sa.String(500), nullable=False),
        sa.Column("mime", sa.String(200), nullable=False, server_default="application/octet-stream"),
        sa.Column("size_bytes", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("blob_key", sa.Text, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        schema="session_orchestrator",
    )

    # Each statement is executed individually because asyncpg (the runtime
    # driver) uses prepared statements and does not allow multiple commands
    # in a single execute() call.
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON session_orchestrator.attachment TO app_user")
    op.execute("ALTER TABLE session_orchestrator.attachment ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE session_orchestrator.attachment FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY attachment_tenant_select ON session_orchestrator.attachment"
        " FOR SELECT TO app_user"
        " USING (tenant_id = current_setting('app.tenant_id', true))"
    )
    op.execute(
        "CREATE POLICY attachment_tenant_insert ON session_orchestrator.attachment"
        " FOR INSERT TO app_user"
        " WITH CHECK (tenant_id = current_setting('app.tenant_id', true))"
    )
    op.execute(
        "CREATE POLICY attachment_tenant_update ON session_orchestrator.attachment"
        " FOR UPDATE TO app_user"
        " USING (tenant_id = current_setting('app.tenant_id', true))"
        " WITH CHECK (tenant_id = current_setting('app.tenant_id', true))"
    )
    op.execute(
        "CREATE POLICY attachment_tenant_delete ON session_orchestrator.attachment"
        " FOR DELETE TO app_user"
        " USING (tenant_id = current_setting('app.tenant_id', true))"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS session_orchestrator.attachment CASCADE")
