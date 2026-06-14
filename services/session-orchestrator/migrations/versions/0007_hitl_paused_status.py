"""Document PAUSED as a valid run.status value for HITL pause/resume.

Revision ID: 0007_hitl_paused_status
Revises: 0006_run_app_snapshot
Create Date: 2026-06-13

Notes
-----
The ``run.status`` column is String(20) with no CHECK constraint (verified
in 0001_init.py — see CREATE TABLE session_orchestrator.run).  Adding
"PAUSED" as a valid status value therefore requires no DDL change.

This migration is intentionally a no-op.  It exists solely as a traceable
record that "PAUSED" is a supported status, documenting the HITL pause/resume
feature (Phase 9).  The orchestrator sets run.status="PAUSED" when the worker
container exits with result.json status="paused"; the run is resumed via the
POST /sessions/{id}/runs/{id}/respond endpoint which re-dispatches to the
scheduler and resets status to "RUNNING".
"""

from collections.abc import Sequence

revision: str = "0007_hitl_paused_status"
down_revision: str | None = "0006_run_app_snapshot"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # No DDL needed — run.status is an unconstrained String(20).
    # See notes above.
    pass


def downgrade() -> None:
    pass
