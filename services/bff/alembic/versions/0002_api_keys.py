"""Create api_keys table with per-user RLS.

Revision ID: 0002_api_keys
Revises:     0001_notification_preferences
Create Date: 2026-06-04
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_api_keys"
down_revision: str | None = "0001_notification_preferences"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "api_keys",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("user_id", sa.String(40), nullable=False, index=True),
        sa.Column("tenant_id", sa.String(40), nullable=False, index=True),
        sa.Column("name", sa.String(120), nullable=False),
        # First 16 chars of the raw key — safe to display
        sa.Column("key_prefix", sa.String(40), nullable=False),
        # SHA-256 hex digest of the raw key — only thing stored in DB
        sa.Column("key_hash", sa.String(64), nullable=False, unique=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        # Nullable: updated by the API key auth middleware on use
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        # Nullable: None = active, set = revoked (soft delete)
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )

    # Grant the runtime role access.  The GRANT USAGE on schema agent_bff was
    # already issued by migration 0001_notification_preferences; we only need
    # DML on the new table.
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON api_keys TO app_user")

    # Row-level security: each user only sees their own keys.
    op.execute("ALTER TABLE api_keys ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE api_keys FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY api_keys_select ON api_keys"
        " FOR SELECT TO app_user"
        " USING (user_id = current_setting('app.user_id', true))"
    )
    op.execute(
        "CREATE POLICY api_keys_insert ON api_keys"
        " FOR INSERT TO app_user"
        " WITH CHECK (user_id = current_setting('app.user_id', true))"
    )
    op.execute(
        "CREATE POLICY api_keys_update ON api_keys"
        " FOR UPDATE TO app_user"
        " USING (user_id = current_setting('app.user_id', true))"
        " WITH CHECK (user_id = current_setting('app.user_id', true))"
    )
    op.execute(
        "CREATE POLICY api_keys_delete ON api_keys"
        " FOR DELETE TO app_user"
        " USING (user_id = current_setting('app.user_id', true))"
    )
    # Special policy: allow the full-table scan needed for the auth-middleware lookup.
    # The middleware hashes the presented key and looks it up by hash across ALL users
    # (it doesn't know which user owns the key yet).  We grant a second bypass policy
    # under a dedicated role-level check.  Since the app_user role itself executes this
    # query we use a PERMISSIVE policy that allows matching by key_hash when
    # app.actor = 'APIKEY_LOOKUP' — an in-transaction marker set by the middleware.
    op.execute(
        "CREATE POLICY api_keys_hash_lookup ON api_keys"
        " FOR SELECT TO app_user"
        " USING (current_setting('app.actor', true) = 'APIKEY_LOOKUP')"
    )


def downgrade() -> None:
    op.drop_table("api_keys")
