"""Add execution_mode column to run to distinguish inline vs sandbox runs.

Revision ID: 0010_run_execution_mode
Revises: 0009_canvas
Create Date: 2026-07-10

Notes
-----
Adds a non-nullable ``execution_mode`` column (String(16), server_default
"sandbox") to the ``run`` table so chat-mode "inline" runs — executed in the
orchestrator process by streaming directly from the LLM gateway, no sandbox
container — can be told apart from ordinary "sandbox" runs.

Existing rows are back-filled to "sandbox" via the server_default, preserving
the current behaviour: a run with no explicit mode is a sandbox run.

Deliberately NOT named ``runtime`` — the scheduler's ``RuntimeKind`` already
owns that term (docker vs cube microVM); ``execution_mode`` is an orthogonal
axis (inline vs sandbox) owned by the orchestrator.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010_run_execution_mode"
down_revision: str | None = "0009_canvas"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "run",
        sa.Column(
            "execution_mode",
            sa.String(16),
            nullable=False,
            server_default="sandbox",
        ),
        schema="session_orchestrator",
    )


def downgrade() -> None:
    op.drop_column("run", "execution_mode", schema="session_orchestrator")
