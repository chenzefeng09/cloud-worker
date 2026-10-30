"""Create web_sessions table — server-side browser sessions with per-user RLS.

Revision ID: 0005_web_sessions
Revises:     0004_breach_records
Create Date: 2026-10-30

The signed session cookie now carries only an opaque session id; the
(encrypted) access and refresh tokens it maps to live here so the BFF can
refresh them before the 5-minute access token expires.  See
``agent_bff.domain.web_sessions``.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0005_web_sessions"
down_revision: str | None = "0004_breach_records"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OWNER = (
    "user_id = current_setting('app.user_id', true)"
    " AND tenant_id = current_setting('app.tenant_id', true)"
)


def upgrade() -> None:
    op.create_table(
        "web_sessions",
        # SHA-256 hex of the session id in the cookie — the raw id is not stored.
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(40), nullable=False),
        sa.Column("user_id", sa.String(40), nullable=False),
        # Fernet ciphertexts, bound to (id, tenant, user, kind).
        sa.Column("access_token_enc", sa.Text, nullable=False),
        sa.Column("access_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("refresh_token_enc", sa.Text, nullable=True),
        sa.Column("clearance", sa.String(8), nullable=False, server_default=""),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("refreshed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_web_sessions_tenant_user", "web_sessions", ["tenant_id", "user_id"])
    op.create_index("ix_web_sessions_expires_at", "web_sessions", ["expires_at"])

    # The GRANT USAGE on schema agent_bff was issued by 0001.
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON web_sessions TO app_user")

    # Row-level security: a request only reaches the session rows of the
    # tenant + user its signed cookie names.
    op.execute("ALTER TABLE web_sessions ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE web_sessions FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY web_sessions_select ON web_sessions FOR SELECT TO app_user USING ({_OWNER})"
    )
    op.execute(
        "CREATE POLICY web_sessions_insert ON web_sessions FOR INSERT TO app_user"
        f" WITH CHECK ({_OWNER})"
    )
    op.execute(
        "CREATE POLICY web_sessions_update ON web_sessions FOR UPDATE TO app_user"
        f" USING ({_OWNER}) WITH CHECK ({_OWNER})"
    )
    op.execute(
        f"CREATE POLICY web_sessions_delete ON web_sessions FOR DELETE TO app_user USING ({_OWNER})"
    )
    # Housekeeping: the opportunistic sweep (actor SESSION_SWEEP) may delete
    # any row that has already passed its hard expiry — nothing else.
    op.execute(
        "CREATE POLICY web_sessions_sweep ON web_sessions FOR DELETE TO app_user"
        " USING (current_setting('app.actor', true) = 'SESSION_SWEEP'"
        " AND expires_at < now())"
    )


def downgrade() -> None:
    op.drop_index("ix_web_sessions_expires_at", table_name="web_sessions")
    op.drop_index("ix_web_sessions_tenant_user", table_name="web_sessions")
    op.drop_table("web_sessions")
