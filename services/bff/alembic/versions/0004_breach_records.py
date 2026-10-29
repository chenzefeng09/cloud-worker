"""Create breach_records table with per-tenant RLS.

Revision ID: 0004_breach_records
Revises:     0003_privacy_notice_acks
Create Date: 2026-10-30

The controller's personal-data breach register (GDPR Art. 33(5)), managed from
the admin Compliance page via ``/admin/compliance/breaches``.  Rows are a
compliance record kept for the authority, so the runtime role gets no DELETE
grant; a breach is closed, never removed.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0004_breach_records"
down_revision: str | None = "0003_privacy_notice_acks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "breach_records",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("tenant_id", sa.String(40), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("description", sa.Text, nullable=False, server_default=""),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=True),
        # Starts the Art. 33(1) 72-hour notification clock.
        sa.Column("aware_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="open"),
        sa.Column("risk_level", sa.String(16), nullable=False, server_default="medium"),
        sa.Column(
            "data_categories",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("approx_subjects", sa.Integer, nullable=True),
        sa.Column("approx_records", sa.Integer, nullable=True),
        sa.Column("likely_consequences", sa.Text, nullable=False, server_default=""),
        sa.Column("measures_taken", sa.Text, nullable=False, server_default=""),
        sa.Column("authority_notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("authority_reference", sa.String(200), nullable=True),
        sa.Column("subjects_notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delay_reason", sa.Text, nullable=True),
        sa.Column("security_incident_id", sa.String(64), nullable=True),
        sa.Column("created_by", sa.String(40), nullable=False),
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
        sa.CheckConstraint(
            "status IN ('open', 'contained', 'closed')", name="ck_breach_records_status",
        ),
        sa.CheckConstraint(
            "risk_level IN ('low', 'medium', 'high')", name="ck_breach_records_risk_level",
        ),
    )
    op.create_index(
        "ix_breach_records_tenant_aware", "breach_records", ["tenant_id", "aware_at"],
    )

    # The GRANT USAGE on schema agent_bff was issued by 0001.  No DELETE: the
    # register is evidence for the supervisory authority.
    op.execute("GRANT SELECT, INSERT, UPDATE ON breach_records TO app_user")

    # Row-level security: a tenant only sees its own register.
    op.execute("ALTER TABLE breach_records ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE breach_records FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY breach_records_select ON breach_records"
        " FOR SELECT TO app_user"
        " USING (tenant_id = current_setting('app.tenant_id', true))"
    )
    op.execute(
        "CREATE POLICY breach_records_insert ON breach_records"
        " FOR INSERT TO app_user"
        " WITH CHECK (tenant_id = current_setting('app.tenant_id', true))"
    )
    op.execute(
        "CREATE POLICY breach_records_update ON breach_records"
        " FOR UPDATE TO app_user"
        " USING (tenant_id = current_setting('app.tenant_id', true))"
        " WITH CHECK (tenant_id = current_setting('app.tenant_id', true))"
    )


def downgrade() -> None:
    op.drop_index("ix_breach_records_tenant_aware", table_name="breach_records")
    op.drop_table("breach_records")
