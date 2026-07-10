"""Canvas v2 — first-class canvas documents, versions, and suggestions.

Revision ID: 0009_canvas
Revises: 0008_run_source
Create Date: 2026-07-10

Notes
-----
Three new tables plus a ``message.canvas_id`` column, per the canvas-v2 design
(``docs/canvas-v2-design.md`` §3).

* ``canvas_document`` — the canvas as a first-class object (id ``cvs_…``),
  scoped to a session so it rides the session's RLS / retention / soft-delete.
* ``canvas_version`` — an immutable full-blocks snapshot (id ``cvv_…``); every
  change (model accept / manual save / revert) appends a new version.
  ``UNIQUE(canvas_id, version)`` gives the optimistic-lock its authority.
* ``canvas_suggestion`` — a proposed edit (id ``csg_…``); ``diffs`` is applied
  only on accept, inside one transaction with a ``base_version`` guard.

Same RLS discipline as 0001_init / 0002_attachment: enable + FORCE RLS, four
explicit per-command policies (SELECT / INSERT / UPDATE / DELETE, never
FOR ALL) scoped ``TO app_user`` on the ``tenant_id = current_setting(
'app.tenant_id', true)`` predicate.

asyncpg (the runtime driver) uses prepared statements and rejects multiple SQL
commands in a single ``execute()`` — so every GRANT / ALTER / CREATE POLICY is
its own ``op.execute`` call.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009_canvas"
down_revision: str | None = "0008_run_source"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _enable_rls(table: str) -> None:
    """Enable + force RLS and install the four per-command tenant policies.

    Each statement is a separate ``op.execute`` (asyncpg forbids multiple
    commands per execute).  Policy predicate mirrors 0002_attachment exactly.
    """
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
    # ── canvas_document ──────────────────────────────────────────────────────
    op.create_table(
        "canvas_document",
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
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("current_version", sa.Integer, nullable=False, server_default="1"),
        sa.Column("source_message_id", sa.String(30), nullable=True),
        sa.Column("source_run_id", sa.String(30), nullable=True),
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
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        schema="session_orchestrator",
    )
    _enable_rls("canvas_document")

    # ── canvas_version ───────────────────────────────────────────────────────
    op.create_table(
        "canvas_version",
        sa.Column("id", sa.String(30), primary_key=True),
        sa.Column("tenant_id", sa.String(30), nullable=False, index=True),
        sa.Column(
            "canvas_id",
            sa.String(30),
            sa.ForeignKey("session_orchestrator.canvas_document.id"),
            nullable=False,
            index=True,
        ),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("blocks", postgresql.JSONB, nullable=False),
        sa.Column("change_summary", sa.Text, nullable=True),
        sa.Column("created_by", sa.String(10), nullable=False),
        sa.Column("base_version", sa.Integer, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("canvas_id", "version", name="uq_canvas_version_canvas_version"),
        schema="session_orchestrator",
    )
    _enable_rls("canvas_version")

    # ── canvas_suggestion ────────────────────────────────────────────────────
    op.create_table(
        "canvas_suggestion",
        sa.Column("id", sa.String(30), primary_key=True),
        sa.Column("tenant_id", sa.String(30), nullable=False, index=True),
        sa.Column(
            "canvas_id",
            sa.String(30),
            sa.ForeignKey("session_orchestrator.canvas_document.id"),
            nullable=False,
            index=True,
        ),
        sa.Column("base_version", sa.Integer, nullable=False),
        sa.Column("status", sa.String(10), nullable=False, server_default="proposed"),
        sa.Column("instruction", sa.Text, nullable=False),
        sa.Column("diffs", postgresql.JSONB, nullable=False),
        sa.Column("change_summary", sa.Text, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        schema="session_orchestrator",
    )
    _enable_rls("canvas_suggestion")

    # ── message.canvas_id ────────────────────────────────────────────────────
    # Phase-A materialization backfills this on the source message; Phase-B
    # model replies carry it directly.  NULL for all pre-canvas messages.
    op.add_column(
        "message",
        sa.Column("canvas_id", sa.String(30), nullable=True),
        schema="session_orchestrator",
    )


def downgrade() -> None:
    op.drop_column("message", "canvas_id", schema="session_orchestrator")
    # CASCADE drops the policies with the table; child tables first to respect
    # the FK ordering (version/suggestion → document).
    op.execute("DROP TABLE IF EXISTS session_orchestrator.canvas_suggestion CASCADE")
    op.execute("DROP TABLE IF EXISTS session_orchestrator.canvas_version CASCADE")
    op.execute("DROP TABLE IF EXISTS session_orchestrator.canvas_document CASCADE")
