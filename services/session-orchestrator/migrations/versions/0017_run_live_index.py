"""Partial index over live runs for the completion watcher and the sweeper.

Revision ID: 0017_run_live_index
Revises: 0016_user_privacy_settings
Create Date: 2026-10-30

Notes
-----
The completion watcher (``services/reconcile_sweeper.py``) looks up every
RUNNING / PENDING run across tenants every couple of seconds, and the stale-run
sweeper does the same once a minute.  Without an index each lookup scans the
whole ``run`` table, which only grows.  Live runs are a tiny slice of it, so a
partial index keyed on ``status`` stays small however long the history gets.

The index rides the table's existing grants and RLS policies (0001_init).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017_run_live_index"
down_revision: str | None = "0016_user_privacy_settings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_run_live",
        "run",
        ["status"],
        schema="session_orchestrator",
        postgresql_where=sa.text("status IN ('RUNNING', 'PENDING')"),
    )


def downgrade() -> None:
    op.drop_index("ix_run_live", table_name="run", schema="session_orchestrator")
