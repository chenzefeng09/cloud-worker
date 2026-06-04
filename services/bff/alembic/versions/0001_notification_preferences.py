"""Create notification_preferences table.

Revision ID: 0001_notification_preferences
Revises:
Create Date: 2026-06-04
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_notification_preferences"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "notification_preferences",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("tenant_id", sa.String(40), nullable=False, index=True),
        sa.Column("user_id", sa.String(40), nullable=False, index=True),
        sa.Column("event_key", sa.String(80), nullable=False),
        sa.Column("email_enabled", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column("push_enabled", sa.Boolean, nullable=False, server_default=sa.text("true")),
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
        sa.UniqueConstraint("user_id", "event_key", name="uq_notif_user_event"),
    )

    # Grant the runtime role access.  app_migrator owns the schema;
    # app_user (the non-owner runtime role) needs USAGE + DML.
    op.execute("GRANT USAGE ON SCHEMA agent_bff TO app_user")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON notification_preferences TO app_user")

    # Row-level security: each user only sees their own rows.
    op.execute("ALTER TABLE notification_preferences ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE notification_preferences FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY notif_pref_select ON notification_preferences"
        " FOR SELECT TO app_user"
        " USING (user_id = current_setting('app.user_id', true))"
    )
    op.execute(
        "CREATE POLICY notif_pref_insert ON notification_preferences"
        " FOR INSERT TO app_user"
        " WITH CHECK (user_id = current_setting('app.user_id', true))"
    )
    op.execute(
        "CREATE POLICY notif_pref_update ON notification_preferences"
        " FOR UPDATE TO app_user"
        " USING (user_id = current_setting('app.user_id', true))"
        " WITH CHECK (user_id = current_setting('app.user_id', true))"
    )
    op.execute(
        "CREATE POLICY notif_pref_delete ON notification_preferences"
        " FOR DELETE TO app_user"
        " USING (user_id = current_setting('app.user_id', true))"
    )


def downgrade() -> None:
    op.drop_table("notification_preferences")
