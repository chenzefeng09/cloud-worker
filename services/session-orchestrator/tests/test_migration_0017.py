"""Migration 0017 rendered to PostgreSQL SQL offline (no database needed)."""

from __future__ import annotations

import importlib.util
import io
import pathlib
from types import ModuleType

from alembic.migration import MigrationContext
from alembic.operations import Operations

from agent_session_orchestrator.services import reconcile_sweeper

_PATH = (
    pathlib.Path(__file__).resolve().parents[1]
    / "migrations" / "versions" / "0017_run_live_index.py"
)


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("mig_0017", _PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _render(fn_name: str) -> str:
    module = _load()
    buf = io.StringIO()
    ctx = MigrationContext.configure(
        dialect_name="postgresql", opts={"as_sql": True, "output_buffer": buf},
    )
    with Operations.context(ctx):
        getattr(module, fn_name)()
    return " ".join(buf.getvalue().split())


def test_revision_chain() -> None:
    module = _load()
    assert module.revision == "0017_run_live_index"
    assert module.down_revision == "0016_user_privacy_settings"


def test_upgrade_indexes_live_runs_only() -> None:
    sql = _render("upgrade")
    assert (
        "CREATE INDEX ix_run_live ON session_orchestrator.run (status)"
        " WHERE status IN ('RUNNING', 'PENDING')"
    ) in sql


def test_the_scans_filter_on_the_indexed_predicate() -> None:
    # The planner uses a partial index only for queries that imply its WHERE.
    predicate = "r.status IN ('RUNNING', 'PENDING')"
    assert predicate in reconcile_sweeper._STALE_RUNS_SQL
    assert predicate in reconcile_sweeper._LIVE_SANDBOX_RUNS_SQL


def test_downgrade_drops_the_index() -> None:
    assert "DROP INDEX session_orchestrator.ix_run_live" in _render("downgrade")
