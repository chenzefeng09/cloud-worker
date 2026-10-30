"""Migration 0016 rendered to PostgreSQL SQL offline (no database needed)."""

from __future__ import annotations

import importlib.util
import io
import pathlib
from types import ModuleType

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from agent_session_orchestrator.domain.models import UserPrivacySettings

_PATH = (
    pathlib.Path(__file__).resolve().parents[1]
    / "migrations" / "versions" / "0016_user_privacy_settings.py"
)


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("mig_0016", _PATH)
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
    assert module.revision == "0016_user_privacy_settings"
    assert module.down_revision == "0015_workspace_file_version"


def test_upgrade_creates_table_with_rls_and_sweep_indexes() -> None:
    sql = _render("upgrade")
    assert "CREATE TABLE session_orchestrator.user_privacy_settings" in sql
    assert "memory_enabled BOOLEAN DEFAULT true NOT NULL" in sql
    assert "updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL" in sql
    assert "CONSTRAINT pk_user_privacy_settings PRIMARY KEY (tenant_id, user_id)" in sql

    qualified = "session_orchestrator.user_privacy_settings"
    assert f"GRANT SELECT, INSERT, UPDATE, DELETE ON {qualified} TO app_user" in sql
    assert f"ALTER TABLE {qualified} ENABLE ROW LEVEL SECURITY" in sql
    assert f"ALTER TABLE {qualified} FORCE ROW LEVEL SECURITY" in sql
    predicate = "tenant_id = current_setting('app.tenant_id', true)"
    for command in ("SELECT", "INSERT", "UPDATE", "DELETE"):
        policy = f"CREATE POLICY user_privacy_settings_tenant_{command.lower()} ON {qualified}"
        assert f"{policy} FOR {command} TO app_user" in sql
    assert f"FOR UPDATE TO app_user USING ({predicate}) WITH CHECK ({predicate})" in sql
    assert f"FOR INSERT TO app_user WITH CHECK ({predicate})" in sql

    assert (
        "CREATE INDEX ix_session_deleted_at ON session_orchestrator.session (deleted_at)"
        " WHERE deleted_at IS NOT NULL"
    ) in sql
    assert (
        "CREATE INDEX ix_session_updated_at_live ON session_orchestrator.session (updated_at)"
        " WHERE deleted_at IS NULL"
    ) in sql


def test_downgrade_drops_everything() -> None:
    sql = _render("downgrade")
    assert "DROP INDEX session_orchestrator.ix_session_updated_at_live" in sql
    assert "DROP INDEX session_orchestrator.ix_session_deleted_at" in sql
    assert "DROP TABLE IF EXISTS session_orchestrator.user_privacy_settings CASCADE" in sql


def test_model_matches_migration_shape() -> None:
    ddl = " ".join(
        str(CreateTable(UserPrivacySettings.__table__).compile(dialect=postgresql.dialect()))
        .split()
    )
    assert "CREATE TABLE session_orchestrator.user_privacy_settings" in ddl
    assert "PRIMARY KEY (tenant_id, user_id)" in ddl
    assert "memory_enabled BOOLEAN DEFAULT 'true' NOT NULL" in ddl or (
        "memory_enabled BOOLEAN DEFAULT true NOT NULL" in ddl
    )
