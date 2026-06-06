"""Add input_tokens / output_tokens / total_tokens columns to run.

Revision ID: 0004_run_tokens
Revises: 0003_artifact
Create Date: 2026-06-04

Notes
-----
These three nullable Integer columns persist token usage reported by the
scheduler's ``GET /runs/{id}`` response under ``usage.{input,output,total}_tokens``.
They are written by the orchestrator's reconcile path when a run reaches
COMPLETED or FAILED status.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_run_tokens"
down_revision: str | None = "0003_artifact"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "run",
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        schema="session_orchestrator",
    )
    op.add_column(
        "run",
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        schema="session_orchestrator",
    )
    op.add_column(
        "run",
        sa.Column("total_tokens", sa.Integer(), nullable=True),
        schema="session_orchestrator",
    )


def downgrade() -> None:
    op.drop_column("run", "total_tokens", schema="session_orchestrator")
    op.drop_column("run", "output_tokens", schema="session_orchestrator")
    op.drop_column("run", "input_tokens", schema="session_orchestrator")
