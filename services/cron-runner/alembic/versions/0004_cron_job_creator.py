"""Record who created each trigger (``cron_jobs.created_by_user_id``).

Scheduled and webhook runs fire as the cron service account, so nothing tied a
trigger to the person who set it up: its runs were not attributable, and a
user's data export or erasure could not find their triggers.  Existing rows
stay NULL — their creator was never recorded.

Revision ID: 0004_cron_job_creator
Revises: 0003_timezone_default_utc
Create Date: 2026-10-30
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0004_cron_job_creator"
down_revision: str | None = "0003_timezone_default_utc"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "cron_jobs",
        sa.Column("created_by_user_id", sa.String(40), nullable=True),
    )
    op.create_index(
        "ix_cron_jobs_tenant_created_by",
        "cron_jobs",
        ["tenant_id", "created_by_user_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_cron_jobs_tenant_created_by", table_name="cron_jobs")
    op.drop_column("cron_jobs", "created_by_user_id")
