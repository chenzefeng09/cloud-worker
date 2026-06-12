"""Add ``pinned_version_id`` to user_installed_skills.

A user may either follow the latest version of a skill (``pinned_version_id IS
NULL``) or pin a specific version they've installed.  Stage-two skill loading
on the scheduler reads this column to decide which version blob to materialise
into the host content-addressed cache before mounting it into the sandbox.

Revision ID: 0003_pinned_version
Revises: 0002_user_installed_skill
Create Date: 2026-06-11
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_pinned_version"
down_revision: str | None = "0002_user_installed_skill"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "user_installed_skills",
        sa.Column(
            "pinned_version_id",
            sa.String(40),
            sa.ForeignKey("skill_versions.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("user_installed_skills", "pinned_version_id")
