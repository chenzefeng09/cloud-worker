"""Per-user privacy settings + trash/retention sweep indexes.

Revision ID: 0016_user_privacy_settings
Revises: 0015_workspace_file_version
Create Date: 2026-10-30

Notes
-----
* ``user_privacy_settings`` — one row per ``(tenant_id, user_id)`` holding the
  user's GDPR data-owner preferences.  Today that is only ``memory_enabled``
  (long-term memory opt-out, default true); a missing row means "defaults".
* ``ix_session_deleted_at`` — partial index over soft-deleted sessions so the
  trash hard-purge (``deleted_at < now() - trash_grace_days``) does not scan
  the live table.
* ``ix_session_updated_at_live`` — partial index over live sessions for the
  inactivity retention sweep, which keys on ``updated_at``.

Same RLS discipline as 0015_workspace_file_version: enable + FORCE, four
per-command policies scoped TO app_user on tenant_id.  Every GRANT / ALTER /
CREATE POLICY is its own ``op.execute`` (asyncpg rejects multi-command SQL).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016_user_privacy_settings"
down_revision: str | None = "0015_workspace_file_version"
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
    # ── user_privacy_settings ───────────────────────────────────────────────
    op.create_table(
        "user_privacy_settings",
        sa.Column("tenant_id", sa.String(30), nullable=False),
        sa.Column("user_id", sa.String(30), nullable=False),
        sa.Column(
            "memory_enabled",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("tenant_id", "user_id", name="pk_user_privacy_settings"),
        schema="session_orchestrator",
    )
    _enable_rls("user_privacy_settings")

    # ── retention / trash sweep indexes ─────────────────────────────────────
    op.create_index(
        "ix_session_deleted_at",
        "session",
        ["deleted_at"],
        schema="session_orchestrator",
        postgresql_where=sa.text("deleted_at IS NOT NULL"),
    )
    op.create_index(
        "ix_session_updated_at_live",
        "session",
        ["updated_at"],
        schema="session_orchestrator",
        postgresql_where=sa.text("deleted_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_session_updated_at_live", table_name="session", schema="session_orchestrator",
    )
    op.drop_index("ix_session_deleted_at", table_name="session", schema="session_orchestrator")
    op.execute("DROP TABLE IF EXISTS session_orchestrator.user_privacy_settings CASCADE")
