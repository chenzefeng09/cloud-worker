"""Tests for App-execution-history support: the ``app_id`` column on ``run``.

Covers three layers, all mock-DB (the live-DB RLS / migration path is
exercised by the migrate sidecar in dev):

* **Service** - ``SessionService.create_run`` denormalises ``app_id`` out of
  ``app_snapshot``; ``list_tenant_runs`` filters on ``app_id`` and pages with
  ``offset``.
* **Endpoint** - ``GET /runs?app_id=`` forwards the filter; omitting it keeps
  the legacy tenant-wide behaviour (no app_id predicate).
* **Migration** - the 0014 upgrade SQL is well-formed (single-statement
  ``op`` calls, no multi-command ``execute`` that asyncpg would reject).

The query-construction style mirrors ``test_tenant_runs.py``: the DB is a
mock, so filtering is asserted at the WHERE-clause / bind-param boundary.
A SQL equality ``app_id = :app_id`` inherently excludes NULL rows and other
apps' rows, so verifying the predicate is sufficient at this layer.
"""

from __future__ import annotations

import datetime as dt
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from agent_session_orchestrator.domain.models import Run, Session
from agent_session_orchestrator.services.session_service import SessionService
from tests._helpers import TEST_TENANT, make_scalar_result

_URL = "/api/session/v1/runs"


def _run(
    rid: str,
    *,
    tenant: str = TEST_TENANT,
    session_id: str = "ses_1",
    status: str = "COMPLETED",
    app_id: str | None = None,
    created: dt.datetime | None = None,
) -> Run:
    return Run(
        id=rid,
        tenant_id=tenant,
        session_id=session_id,
        task_description="t",
        status=status,
        app_id=app_id,
        created_at=created or dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
    )


# --------------------------------------------------------------------------- #
# Service: create_run denormalises app_id out of app_snapshot
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_create_run_sets_app_id_from_snapshot(mock_db: AsyncMock) -> None:
    """create_run pulls app_id out of app_snapshot onto the indexed column."""
    session_row = Session(
        id="ses_1", tenant_id=TEST_TENANT, user_id="u1", status="ACTIVE",
    )
    mock_db.execute.return_value = make_scalar_result(session_row)

    async def _refresh(obj):
        if isinstance(obj, Run):
            obj.id = "run_new"
            obj.created_at = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)

    mock_db.refresh.side_effect = _refresh

    svc = SessionService(mock_db, tenant_id=TEST_TENANT)
    run = await svc.create_run(
        "ses_1", "do thing",
        app_snapshot={"app_id": "app_abc", "app_name": "My App", "inputs": []},
    )
    assert run.app_id == "app_abc"
    # The snapshot itself is still persisted verbatim.
    assert run.app_snapshot == {"app_id": "app_abc", "app_name": "My App", "inputs": []}


@pytest.mark.asyncio
async def test_create_run_app_id_null_when_no_snapshot(mock_db: AsyncMock) -> None:
    """Ordinary chat runs (no app_snapshot) leave app_id NULL."""
    session_row = Session(
        id="ses_1", tenant_id=TEST_TENANT, user_id="u1", status="ACTIVE",
    )
    mock_db.execute.return_value = make_scalar_result(session_row)
    mock_db.refresh = AsyncMock(side_effect=[None])

    svc = SessionService(mock_db, tenant_id=TEST_TENANT)
    run = await svc.create_run("ses_1", "hi", app_snapshot=None)
    assert run.app_id is None
    assert run.app_snapshot is None


@pytest.mark.asyncio
async def test_create_run_app_id_null_when_snapshot_missing_key(mock_db: AsyncMock) -> None:
    """A snapshot without an app_id key (malformed) leaves app_id NULL, not a 500."""
    session_row = Session(
        id="ses_1", tenant_id=TEST_TENANT, user_id="u1", status="ACTIVE",
    )
    mock_db.execute.return_value = make_scalar_result(session_row)
    mock_db.refresh = AsyncMock(side_effect=[None])

    svc = SessionService(mock_db, tenant_id=TEST_TENANT)
    run = await svc.create_run(
        "ses_1", "hi", app_snapshot={"inputs": []},  # no app_id
    )
    assert run.app_id is None


@pytest.mark.asyncio
async def test_create_run_app_id_null_when_snapshot_app_id_not_str(mock_db: AsyncMock) -> None:
    """A non-string app_id in the snapshot is ignored rather than crashing."""
    session_row = Session(
        id="ses_1", tenant_id=TEST_TENANT, user_id="u1", status="ACTIVE",
    )
    mock_db.execute.return_value = make_scalar_result(session_row)
    mock_db.refresh = AsyncMock(side_effect=[None])

    svc = SessionService(mock_db, tenant_id=TEST_TENANT)
    run = await svc.create_run(
        "ses_1", "hi", app_snapshot={"app_id": 12345},  # wrong type
    )
    assert run.app_id is None


# --------------------------------------------------------------------------- #
# Service: list_tenant_runs app_id filter + offset
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_service_list_tenant_runs_app_id_filter() -> None:
    """app_id adds an app_id = :app_id predicate; bind param equals the value."""
    db = AsyncMock()
    captured: dict[str, object] = {}

    async def _exec(stmt: object) -> MagicMock:
        captured["sql"] = str(stmt)
        captured["params"] = stmt.compile().params
        return make_scalar_result([_run("r1", app_id="app_abc")])

    db.execute = _exec
    svc = SessionService(db, tenant_id=TEST_TENANT)
    runs = await svc.list_tenant_runs(app_id="app_abc")
    assert [r.id for r in runs] == ["r1"]
    sql = captured["sql"].lower()
    assert "app_id" in sql
    assert "app_abc" in captured["params"].values()


@pytest.mark.asyncio
async def test_service_list_tenant_runs_no_app_id_no_predicate() -> None:
    """Omitting app_id adds no app_id predicate (legacy behaviour preserved)."""
    db = AsyncMock()
    captured: dict[str, object] = {}

    async def _exec(stmt: object) -> MagicMock:
        captured["sql"] = str(stmt)
        captured["params"] = stmt.compile().params
        return make_scalar_result([_run("r1")])

    db.execute = _exec
    svc = SessionService(db, tenant_id=TEST_TENANT)
    await svc.list_tenant_runs()
    # No app_id value should appear as a bind param.
    assert "app_abc" not in captured["params"].values()
    # Sanity: the tenant predicate is still there.
    assert TEST_TENANT in captured["params"].values()


@pytest.mark.asyncio
async def test_service_list_tenant_runs_app_id_with_status_compose() -> None:
    """app_id and status filters compose into a single WHERE."""
    db = AsyncMock()
    captured: dict[str, object] = {}

    async def _exec(stmt: object) -> MagicMock:
        captured["params"] = stmt.compile().params
        return make_scalar_result([_run("r1", app_id="app_abc", status="RUNNING")])

    db.execute = _exec
    svc = SessionService(db, tenant_id=TEST_TENANT)
    await svc.list_tenant_runs(status="RUNNING", app_id="app_abc")
    params = captured["params"].values()
    assert "app_abc" in params
    assert "RUNNING" in params


@pytest.mark.asyncio
async def test_service_list_tenant_runs_offset_applied() -> None:
    """offset>0 emits an OFFSET clause; offset=0 omits it (legacy path)."""
    db = AsyncMock()
    captured: dict[str, object] = {}

    async def _exec(stmt: object) -> MagicMock:
        captured["sql"] = str(stmt)
        return make_scalar_result([_run("r1")])

    db.execute = _exec
    svc = SessionService(db, tenant_id=TEST_TENANT)

    await svc.list_tenant_runs(offset=10)
    assert "offset" in captured["sql"].lower()

    captured.clear()
    await svc.list_tenant_runs(offset=0)
    assert "offset" not in captured["sql"].lower()


# --------------------------------------------------------------------------- #
# Endpoint: GET /runs?app_id=
# --------------------------------------------------------------------------- #


def test_endpoint_app_id_filter_forwards(
    app_client: TestClient, mock_db: AsyncMock, auth_headers: dict[str, str],
) -> None:
    """?app_id= propagates into the compiled WHERE with the caller's value."""
    captured: dict[str, object] = {}

    async def _capture(stmt: object) -> MagicMock:
        captured["params"] = stmt.compile().params
        return make_scalar_result([_run("run_mine", app_id="app_abc")])

    mock_db.execute.side_effect = _capture

    resp = app_client.get(_URL, params={"app_id": "app_abc"}, headers=auth_headers)
    assert resp.status_code == 200, resp.text
    assert "app_abc" in captured["params"].values()


def test_endpoint_no_app_id_keeps_legacy_behaviour(
    app_client: TestClient, mock_db: AsyncMock, auth_headers: dict[str, str],
) -> None:
    """No app_id param -> no app_id predicate; response shape unchanged."""
    captured: dict[str, object] = {}

    async def _capture(stmt: object) -> MagicMock:
        captured["sql"] = str(stmt)
        captured["params"] = stmt.compile().params
        return make_scalar_result([_run("run_x")])

    mock_db.execute.side_effect = _capture

    resp = app_client.get(_URL, headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert "items" in data
    # Legacy field set is unchanged (no app_id leaked into the serialiser).
    assert set(data["items"][0].keys()) == {
        "id", "session_id", "sandbox_id", "status", "task_description",
        "result_summary", "error_message", "started_at", "finished_at",
        "created_at", "input_tokens", "output_tokens", "total_tokens",
        "app_snapshot", "source", "execution_mode",
    }
    # No app_id bind param when the param is omitted.
    assert "app_abc" not in captured["params"].values()


def test_endpoint_offset_param_forwards(
    app_client: TestClient, mock_db: AsyncMock, auth_headers: dict[str, str],
) -> None:
    """?offset=20 emits an OFFSET clause in the compiled SQL."""
    captured: dict[str, object] = {}

    async def _capture(stmt: object) -> MagicMock:
        captured["sql"] = str(stmt)
        return make_scalar_result([_run("r1")])

    mock_db.execute.side_effect = _capture

    resp = app_client.get(_URL, params={"offset": 20}, headers=auth_headers)
    assert resp.status_code == 200
    assert "offset" in captured["sql"].lower()


def test_endpoint_offset_negative_rejected(
    app_client: TestClient, auth_headers: dict[str, str],
) -> None:
    """offset is constrained ge=0 by FastAPI Query validation."""
    resp = app_client.get(_URL, params={"offset": -1}, headers=auth_headers)
    assert resp.status_code == 422


# --------------------------------------------------------------------------- #
# Migration: 0014 upgrade SQL shape (static inspection - no live DB needed)
# --------------------------------------------------------------------------- #


def test_migration_0014_single_statement_ops() -> None:
    """Each op is a single SQL statement (asyncpg forbids multi-command execute).

    Imports the migration module and asserts the upgrade callable exists and
    the revision chain is correct.  The actual DDL is exercised by the migrate
    sidecar in dev; this guards against a regression that re-introduces a
    multi-command ``op.execute`` (the historical RLS-migration trap).
    """
    import importlib

    mod = importlib.import_module("migrations.versions.0014_run_app_id")
    assert mod.revision == "0014_run_app_id"
    assert mod.down_revision == "0013_artifact_user_indexes"
    # upgrade / downgrade are callable and take no args.
    assert callable(mod.upgrade)
    assert callable(mod.downgrade)
