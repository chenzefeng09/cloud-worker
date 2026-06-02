"""Create session_orchestrator schema and tables.

Revision ID: 0001_init
Revises:
Create Date: 2026-05-27

Notes
-----
Tenant isolation follows ``saas-foundation/docs/multi-tenancy.md §7`` exactly:

* RLS is enabled **and forced** on every tenant table so the table owner
  (``app_migrator``) cannot accidentally bypass it from a misconfigured app
  connection.
* Four separate policies (SELECT / INSERT / UPDATE / DELETE) per table —
  ``FOR ALL`` is intentionally avoided because UPDATE needs both ``USING``
  and ``WITH CHECK`` for protection against rewriting ``tenant_id``.
* Policies are scoped ``TO app_user`` (the runtime role); the migration role
  retains BYPASSRLS for schema management only.
* ``current_setting('app.tenant_id', true)`` returns NULL when the session
  variable is missing — combined with ``tenant_id = NULL`` (always false in
  SQL), unauthenticated connections see zero rows by default.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_init"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Tables in this schema that must carry per-tenant RLS.  Order matters only
# for upgrade DDL grouping; the actual policy creation is templated below.
_TENANT_TABLES: tuple[str, ...] = ("session", "message", "run")


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS session_orchestrator")

    op.create_table(
        "session",
        sa.Column("id", sa.String(30), primary_key=True),
        sa.Column("tenant_id", sa.String(30), nullable=False, index=True),
        sa.Column("user_id", sa.String(30), nullable=False, index=True),
        sa.Column("title", sa.String(200), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="ACTIVE"),
        sa.Column("current_run_id", sa.String(30), nullable=True),
        sa.Column("metadata", postgresql.JSONB, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        schema="session_orchestrator",
    )

    op.create_table(
        "message",
        sa.Column("id", sa.String(30), primary_key=True),
        sa.Column("tenant_id", sa.String(30), nullable=False, index=True),
        sa.Column("session_id", sa.String(30), sa.ForeignKey("session_orchestrator.session.id"), nullable=False, index=True),
        sa.Column("role", sa.String(10), nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("tool_calls", postgresql.JSONB, nullable=True),
        sa.Column("run_id", sa.String(30), nullable=True),
        sa.Column("sequence", sa.Integer, nullable=False, default=0),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        schema="session_orchestrator",
    )

    op.create_table(
        "run",
        sa.Column("id", sa.String(30), primary_key=True),
        sa.Column("tenant_id", sa.String(30), nullable=False, index=True),
        sa.Column("session_id", sa.String(30), sa.ForeignKey("session_orchestrator.session.id"), nullable=False, index=True),
        sa.Column("sandbox_id", sa.String(50), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="PENDING"),
        sa.Column("task_description", sa.Text, nullable=False),
        sa.Column("result_summary", sa.Text, nullable=True),
        sa.Column("error_message", sa.Text, nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        schema="session_orchestrator",
    )

    # ------------------------------------------------------------------ RLS
    # Per ``saas-foundation/docs/multi-tenancy.md §7.2``: enable + force RLS,
    # then four explicit policies per table (FOR ALL is intentionally avoided
    # because UPDATE needs both USING and WITH CHECK to block tenant_id
    # rewrites).  app_user is the only runtime role; admin operations get
    # their own policies in a later migration.
    _RLS_TEMPLATE = """
        ALTER TABLE session_orchestrator.{t} ENABLE ROW LEVEL SECURITY;
        ALTER TABLE session_orchestrator.{t} FORCE ROW LEVEL SECURITY;

        CREATE POLICY {t}_tenant_select ON session_orchestrator.{t}
            FOR SELECT TO app_user
            USING (tenant_id = current_setting('app.tenant_id', true));

        CREATE POLICY {t}_tenant_insert ON session_orchestrator.{t}
            FOR INSERT TO app_user
            WITH CHECK (tenant_id = current_setting('app.tenant_id', true));

        CREATE POLICY {t}_tenant_update ON session_orchestrator.{t}
            FOR UPDATE TO app_user
            USING (tenant_id = current_setting('app.tenant_id', true))
            WITH CHECK (tenant_id = current_setting('app.tenant_id', true));

        CREATE POLICY {t}_tenant_delete ON session_orchestrator.{t}
            FOR DELETE TO app_user
            USING (tenant_id = current_setting('app.tenant_id', true));
    """
    for table in _TENANT_TABLES:
        op.execute(_RLS_TEMPLATE.format(t=table))


def downgrade() -> None:
    op.execute("DROP SCHEMA IF EXISTS session_orchestrator CASCADE")
