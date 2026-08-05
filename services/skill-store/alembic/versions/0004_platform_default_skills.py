"""Add ``skills.default_enabled`` + broaden the skills SELECT policy for
platform-global default skills.

Two changes, both in service of the *platform-global default skill set* — a
single set of skills owned by the reserved ``PLATFORM_TENANT_ID`` tenant that
every organization (including future ones) sees and every user gets by default
with zero per-user install rows:

1. ``skills.default_enabled BOOLEAN NOT NULL DEFAULT false`` — marks a platform
   skill as an always-on default (meaningful only on platform-owned rows).

2. Broaden ONLY the ``skills_tenant_select`` RLS policy so any caller can *read*
   platform rows regardless of their ``app.tenant_id``.  The insert/update/
   delete policies are left UNCHANGED: platform rows remain writable only when
   ``app.tenant_id`` equals the sentinel (i.e. exclusively by the seed).

The platform tenant literal is hard-coded here (migrations are historical
artifacts and must not import moving application code).  It MUST stay
byte-for-byte identical to ``skill_store.constants.PLATFORM_TENANT_ID``.

Revision ID: 0004_platform_default_skills
Revises: 0003_pinned_version
Create Date: 2026-07-23

NOTE: the revision id is kept short — ``alembic_version.version_num`` is
``varchar(32)``, so a longer id fails the post-migration version stamp with
``StringDataRightTruncationError``.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0004_platform_default_skills"
down_revision: str | None = "0003_pinned_version"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Keep identical to skill_store.constants.PLATFORM_TENANT_ID.
_PLATFORM_TENANT_ID = "tnt_00000000000000000PLATFORM"


def upgrade() -> None:
    op.add_column(
        "skills",
        sa.Column(
            "default_enabled",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("false"),
        ),
    )

    # Recreate the SELECT policy with the broadened USING clause.  Each statement
    # runs in its own op.execute — asyncpg (the migrator driver) uses prepared
    # statements and rejects multiple commands in a single execute().
    op.execute("DROP POLICY skills_tenant_select ON skills")
    op.execute(
        "CREATE POLICY skills_tenant_select ON skills"
        " FOR SELECT TO app_user"
        " USING ("
        "   tenant_id = current_setting('app.tenant_id', true)"
        f"   OR tenant_id = '{_PLATFORM_TENANT_ID}'"
        " )"
    )


def downgrade() -> None:
    # Restore the original tenant-only SELECT policy.
    op.execute("DROP POLICY skills_tenant_select ON skills")
    op.execute(
        "CREATE POLICY skills_tenant_select ON skills"
        " FOR SELECT TO app_user"
        " USING (tenant_id = current_setting('app.tenant_id', true))"
    )
    op.drop_column("skills", "default_enabled")
