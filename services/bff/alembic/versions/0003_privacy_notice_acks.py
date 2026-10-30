"""Create privacy_notice_acknowledgements table with per-user RLS.

Revision ID: 0003_privacy_notice_acks
Revises:     0002_api_keys
Create Date: 2026-10-30

Append-only log of the privacy-notice versions each user acknowledged
(``POST /me/privacy/acknowledgements``).  The runtime role gets no UPDATE
grant: an acknowledgement is evidence and is never rewritten, only removed by
the user's own erasure request.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0003_privacy_notice_acks"
down_revision: str | None = "0002_api_keys"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "privacy_notice_acknowledgements",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("tenant_id", sa.String(40), nullable=False),
        sa.Column("user_id", sa.String(40), nullable=False),
        # The ``version`` string GET /privacy/notice served at the time.
        sa.Column("notice_version", sa.String(100), nullable=False),
        sa.Column(
            "acknowledged_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_privacy_ack_user_time",
        "privacy_notice_acknowledgements",
        ["tenant_id", "user_id", "acknowledged_at"],
    )

    # The GRANT USAGE on schema agent_bff was issued by 0001; append-only DML
    # here (no UPDATE).
    op.execute(
        "GRANT SELECT, INSERT, DELETE ON privacy_notice_acknowledgements TO app_user"
    )

    # Row-level security: each user only sees their own acknowledgements.
    op.execute("ALTER TABLE privacy_notice_acknowledgements ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE privacy_notice_acknowledgements FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY privacy_ack_select ON privacy_notice_acknowledgements"
        " FOR SELECT TO app_user"
        " USING (user_id = current_setting('app.user_id', true))"
    )
    op.execute(
        "CREATE POLICY privacy_ack_insert ON privacy_notice_acknowledgements"
        " FOR INSERT TO app_user"
        " WITH CHECK (user_id = current_setting('app.user_id', true))"
    )
    op.execute(
        "CREATE POLICY privacy_ack_delete ON privacy_notice_acknowledgements"
        " FOR DELETE TO app_user"
        " USING (user_id = current_setting('app.user_id', true))"
    )


def downgrade() -> None:
    op.drop_index("ix_privacy_ack_user_time", table_name="privacy_notice_acknowledgements")
    op.drop_table("privacy_notice_acknowledgements")
