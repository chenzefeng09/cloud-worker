"""Tests for the tenant-level GET /runs endpoint.

These mirror the mock-DB style used by ``test_messages_runs.py`` /
``test_daily_token_usage.py``: the DB session is a mock, so tenant isolation
and status/limit filtering are asserted at the query-construction boundary
(what the service hands to ``db.execute`` / passes through) plus the HTTP
response shape.  A live-DB end-to-end pass is covered elsewhere by the
``live_db`` suite.
"""

from __future__ import annotations

import datetime as dt
from unittest.mock import AsyncMock, MagicMock

from fastapi.testclient import TestClient

from agent_session_orchestrator.domain.models import Run
from agent_session_orchestrator.services.session_service import SessionService
from tests._helpers import TEST_TENANT, make_scalar_result

_URL = "/api/session/v1/runs"


def _run(
    rid: str,
    *,
    tenant: str = TEST_TENANT,
    session_id: str = "ses_1",
    status: str = "COMPLETED",
    created: dt.datetime | None = None,
) -> Run:
    return Run(
        id=rid,
        tenant_id=tenant,
        session_id=session_id,
        task_description="t",
        status=status,
        created_at=created or dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
    )


def test_tenant_runs_requires_auth(app_client: TestClient) -> None:
    """No X-Tenant-Id → same auth-failure behaviour as other endpoints."""
    resp = app_client.get(_URL)
    assert resp.status_code in (401, 403)


def test_tenant_runs_returns_items_shape(
    app_client: TestClient, mock_db: AsyncMock, auth_headers: dict[str, str],
) -> None:
    """Response is {"items": [...]} with the same fields as the session-level list."""
    runs = [
        _run("run_b", created=dt.datetime(2026, 1, 2, tzinfo=dt.UTC)),
        _run("run_a", created=dt.datetime(2026, 1, 1, tzinfo=dt.UTC)),
    ]
    mock_db.execute.return_value = make_scalar_result(runs)

    resp = app_client.get(_URL, headers=auth_headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert "items" in data
    assert [r["id"] for r in data["items"]] == ["run_b", "run_a"]

    item = data["items"][0]
    # Field-for-field parity with the session-level list serializer.
    expected_keys = {
        "id", "session_id", "sandbox_id", "status", "task_description",
        "result_summary", "error_message", "started_at", "finished_at",
        "created_at", "input_tokens", "output_tokens", "total_tokens",
        "app_snapshot",
    }
    assert set(item.keys()) == expected_keys


def test_tenant_runs_ordered_created_desc(
    app_client: TestClient, mock_db: AsyncMock, auth_headers: dict[str, str],
) -> None:
    """Service issues an order_by created_at DESC; response preserves that order."""
    captured: dict[str, object] = {}

    async def _capture(stmt: object) -> MagicMock:
        captured["stmt"] = stmt
        return make_scalar_result([
            _run("newest", created=dt.datetime(2026, 1, 3, tzinfo=dt.UTC)),
            _run("oldest", created=dt.datetime(2026, 1, 1, tzinfo=dt.UTC)),
        ])

    mock_db.execute.side_effect = _capture

    resp = app_client.get(_URL, headers=auth_headers)
    assert resp.status_code == 200
    assert [r["id"] for r in resp.json()["items"]] == ["newest", "oldest"]
    # The compiled SQL must carry a descending order on created_at.
    sql = str(captured["stmt"]).lower()
    assert "order by" in sql and "created_at desc" in sql


def test_tenant_runs_scoped_to_tenant(
    app_client: TestClient, mock_db: AsyncMock, auth_headers: dict[str, str],
) -> None:
    """The WHERE clause is bound to the caller's tenant, never a foreign one."""
    captured: dict[str, object] = {}

    async def _capture(stmt: object) -> MagicMock:
        captured["params"] = stmt.compile().params
        return make_scalar_result([_run("run_mine")])

    mock_db.execute.side_effect = _capture

    resp = app_client.get(_URL, headers=auth_headers)
    assert resp.status_code == 200
    # tenant_id bind param equals the authenticated tenant (X-Tenant-Id),
    # so a different tenant's rows can never match.
    assert TEST_TENANT in captured["params"].values()


def test_tenant_runs_status_filter_normalised(
    app_client: TestClient, mock_db: AsyncMock, auth_headers: dict[str, str],
) -> None:
    """Lower-case ?status=running is upper-cased before hitting the DB."""
    captured: dict[str, object] = {}

    async def _capture(stmt: object) -> MagicMock:
        captured["params"] = stmt.compile().params
        return make_scalar_result([_run("run_run", status="RUNNING")])

    mock_db.execute.side_effect = _capture

    resp = app_client.get(_URL, params={"status": "running"}, headers=auth_headers)
    assert resp.status_code == 200
    assert "RUNNING" in captured["params"].values()
    assert "running" not in captured["params"].values()


def test_tenant_runs_no_status_no_filter(
    app_client: TestClient, mock_db: AsyncMock, auth_headers: dict[str, str],
) -> None:
    """Omitting status adds no status predicate to the query."""
    captured: dict[str, object] = {}

    async def _capture(stmt: object) -> MagicMock:
        captured["params"] = stmt.compile().params
        return make_scalar_result([_run("run_x")])

    mock_db.execute.side_effect = _capture

    resp = app_client.get(_URL, headers=auth_headers)
    assert resp.status_code == 200
    # Only the tenant bind param is present, not a status one.
    assert "COMPLETED" not in captured["params"].values()


def test_tenant_runs_limit_param_applied(
    app_client: TestClient, mock_db: AsyncMock, auth_headers: dict[str, str],
) -> None:
    """?limit=10 propagates into the compiled LIMIT clause."""
    captured: dict[str, object] = {}

    async def _capture(stmt: object) -> MagicMock:
        captured["sql"] = str(stmt)
        captured["params"] = stmt.compile().params
        return make_scalar_result([_run("run_x")])

    mock_db.execute.side_effect = _capture

    resp = app_client.get(_URL, params={"limit": 10}, headers=auth_headers)
    assert resp.status_code == 200
    assert "limit" in captured["sql"].lower()
    assert 10 in captured["params"].values()


def test_tenant_runs_limit_out_of_range_rejected(
    app_client: TestClient, mock_db: AsyncMock, auth_headers: dict[str, str],
) -> None:
    """limit is constrained ge=1, le=200 by FastAPI Query validation."""
    assert app_client.get(
        _URL, params={"limit": 0}, headers=auth_headers
    ).status_code == 422
    assert app_client.get(
        _URL, params={"limit": 201}, headers=auth_headers
    ).status_code == 422


async def test_service_list_tenant_runs_defaults() -> None:
    """Direct service-layer check: default limit, no status, tenant-scoped."""
    db = AsyncMock()
    captured: dict[str, object] = {}

    async def _exec(stmt: object) -> MagicMock:
        captured["sql"] = str(stmt)
        captured["params"] = stmt.compile().params
        return make_scalar_result([_run("r1"), _run("r2")])

    db.execute = _exec
    svc = SessionService(db, tenant_id=TEST_TENANT)
    runs = await svc.list_tenant_runs()
    assert [r.id for r in runs] == ["r1", "r2"]
    sql = captured["sql"].lower()
    assert "tenant_id" in sql and "limit" in sql
    assert 50 in captured["params"].values()  # default limit
