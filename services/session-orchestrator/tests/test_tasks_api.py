"""Tests for the task board read model (``GET /tasks``, ``GET /tasks/{id}``).

Mock-DB style like the rest of the suite: the API shape and status mapping
are asserted over fake rows, and the SQL is checked by compiling the real
statements against the PostgreSQL dialect (owner scoping, latest-run join,
kind/status predicates).
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql

from agent_session_orchestrator.domain.models import Run, Session
from agent_session_orchestrator.services.task_service import (
    TaskService,
    parse_csv_filter,
    serialize_task,
    task_status_for,
)
from tests._helpers import TEST_TENANT, TEST_USER, make_scalar_result

_URL = "/api/session/v1/tasks"
_T0 = dt.datetime(2026, 10, 1, 12, 0, tzinfo=dt.UTC)


def _session(sid: str = "ses_1", *, kind: str | None = None, spec: dict | None = None,
             updated: dt.datetime = _T0) -> Session:
    meta: dict[str, Any] = {}
    if kind:
        meta["kind"] = kind
    if spec:
        meta["task"] = spec
    return Session(
        id=sid, tenant_id=TEST_TENANT, user_id=TEST_USER, title=f"title {sid}",
        status="ACTIVE", agent_metadata=meta, workspace_id=None,
        created_at=_T0, updated_at=updated,
    )


def _run(rid: str = "run_1", *, status: str = "RUNNING", session_id: str = "ses_1",
         summary: str | None = None) -> Run:
    return Run(
        id=rid, tenant_id=TEST_TENANT, session_id=session_id, status=status,
        task_description="do the thing", result_summary=summary, source="user",
        created_at=_T0, started_at=_T0,
    )


def _rows(rows: list[tuple]) -> MagicMock:
    result = MagicMock()
    result.all = MagicMock(return_value=rows)
    result.first = MagicMock(return_value=rows[0] if rows else None)
    return result


def _sql(stmt: Any) -> str:
    return str(stmt.compile(dialect=postgresql.dialect()))


# ── status / kind mapping ────────────────────────────────────────────────────


def test_task_status_maps_every_run_status() -> None:
    assert task_status_for(None) == "idle"
    assert task_status_for("PENDING") == "queued"
    assert task_status_for("RUNNING") == "running"
    assert task_status_for("PAUSED") == "needs_input"
    assert task_status_for("COMPLETED") == "completed"
    assert task_status_for("FAILED") == "failed"
    assert task_status_for("CANCELLED") == "cancelled"
    assert task_status_for("WHATEVER") == "idle"


def test_parse_csv_filter_validates_values() -> None:
    assert parse_csv_filter(None, ("a", "b"), name="x") is None
    assert parse_csv_filter(" ", ("a", "b"), name="x") is None
    assert parse_csv_filter("A, b", ("a", "b"), name="x") == ["a", "b"]
    try:
        parse_csv_filter("a,zzz", ("a", "b"), name="status")
    except ValueError as exc:
        assert "zzz" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected ValueError")


def test_serialize_task_prefers_task_spec_and_defaults_kind_to_chat() -> None:
    spec = {"instructions": "Research X", "options": {"hitl": False}}
    out = serialize_task(_session(kind="task", spec=spec), _run(summary="done"), 2)
    assert out["kind"] == "task"
    assert out["instructions"] == "Research X"
    assert out["options"] == {"hitl": False}
    assert out["status"] == "running"
    assert out["run_count"] == 2
    assert out["latest_run"]["result_summary"] == "done"

    chat = serialize_task(_session(), _run(), 1)
    assert chat["kind"] == "chat"
    # Without a spec the instructions fall back to the latest run's input.
    assert chat["instructions"] == "do the thing"

    idle = serialize_task(_session(kind="bogus"), None, 0)
    assert idle["kind"] == "chat"
    assert idle["status"] == "idle"
    assert idle["latest_run"] is None


def test_serialize_task_clips_long_summaries() -> None:
    out = serialize_task(_session(), _run(summary="x" * 5000), 1)
    assert len(out["latest_run"]["result_summary"]) == 400
    assert out["latest_run"]["result_summary"].endswith("…")


# ── SQL shape ────────────────────────────────────────────────────────────────


def test_list_query_is_owner_scoped_and_joins_latest_run() -> None:
    svc = TaskService(AsyncMock(), tenant_id=TEST_TENANT, user_id=TEST_USER)
    filters = svc._filters(kinds=["task", "chat"], workspace_id="wsp_1", q="50%_off")
    from sqlalchemy import select

    stmt = (
        select(Session, Run)
        .outerjoin(Run, Run.id == svc._latest_run_id())
        .where(*filters, svc._status_clause(["running", "idle"]))
    )
    sql = _sql(stmt)
    ses, run = "session_orchestrator.session", "session_orchestrator.run"
    assert f"{ses}.user_id = %(user_id_1)s" in sql
    assert f"{ses}.deleted_at IS NULL" in sql
    assert f"LEFT OUTER JOIN {run} ON {run}.id = (SELECT {run}.id" in sql
    assert f"ORDER BY {run}.created_at DESC, {run}.id DESC" in sql
    assert f"({ses}.metadata ->> %(metadata_1)s) IN" in sql
    assert f"({ses}.metadata ->> %(metadata_1)s) IS NULL" in sql
    assert f"{run}.status IN" in sql
    assert f"{run}.id IS NULL" in sql
    assert f"{ses}.title ILIKE" in sql
    params = stmt.compile(dialect=postgresql.dialect()).params
    assert params["user_id_1"] == TEST_USER
    # LIKE metacharacters in the search text are escaped, not interpreted.
    assert any(v == "%50\\%\\_off%" for v in params.values())


# ── HTTP ─────────────────────────────────────────────────────────────────────


def test_list_tasks_returns_items_counts_and_cursor(
    app_client: TestClient, mock_db: AsyncMock, auth_headers: dict[str, str],
) -> None:
    s1 = _session("ses_1", kind="task", spec={"instructions": "A"},
                  updated=_T0 + dt.timedelta(minutes=2))
    s2 = _session("ses_2", updated=_T0 + dt.timedelta(minutes=1))
    s3 = _session("ses_3", updated=_T0)
    mock_db.execute.side_effect = [
        _rows([(s1, _run("run_1", status="PAUSED"), 1), (s2, None, 0), (s3, None, 0)]),
        _rows([("RUNNING", 3), ("PAUSED", 1), (None, 2), ("COMPLETED", 4)]),
    ]

    resp = app_client.get(f"{_URL}?limit=2", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [t["id"] for t in body["items"]] == ["ses_1", "ses_2"]
    assert body["items"][0]["status"] == "needs_input"
    assert body["items"][0]["kind"] == "task"
    assert body["items"][1]["status"] == "idle"
    assert body["next_cursor"] == s2.updated_at.isoformat()
    assert body["counts"]["running"] == 3
    assert body["counts"]["needs_input"] == 1
    assert body["counts"]["idle"] == 2
    assert body["counts"]["completed"] == 4
    assert body["counts"]["failed"] == 0


def test_list_tasks_rejects_unknown_status(
    app_client: TestClient, auth_headers: dict[str, str],
) -> None:
    resp = app_client.get(f"{_URL}?status=running,exploded", headers=auth_headers)
    assert resp.status_code == 422
    assert "exploded" in resp.text


def test_list_tasks_rejects_bad_cursor(
    app_client: TestClient, auth_headers: dict[str, str],
) -> None:
    resp = app_client.get(f"{_URL}?cursor=yesterday", headers=auth_headers)
    assert resp.status_code == 422


def test_list_tasks_requires_auth(app_client: TestClient) -> None:
    resp = app_client.get(_URL)
    assert resp.status_code in (401, 403)


def test_get_task_returns_one(
    app_client: TestClient, mock_db: AsyncMock, auth_headers: dict[str, str],
) -> None:
    mock_db.execute.return_value = _rows([(_session("ses_9"), _run(status="FAILED"), 3)])
    resp = app_client.get(f"{_URL}/ses_9", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "failed"
    assert resp.json()["run_count"] == 3


def test_get_task_404_when_not_owned_or_missing(
    app_client: TestClient, mock_db: AsyncMock, auth_headers: dict[str, str],
) -> None:
    mock_db.execute.return_value = _rows([])
    resp = app_client.get(f"{_URL}/ses_nope", headers=auth_headers)
    assert resp.status_code == 404


# ── PATCH /sessions/{id} with a task spec ────────────────────────────────────


def test_patch_stores_task_spec_on_a_task_session(
    app_client: TestClient, mock_db: AsyncMock, auth_headers: dict[str, str],
) -> None:
    session = _session(kind="task")
    original_meta = session.agent_metadata
    mock_db.execute.return_value = make_scalar_result(session)

    resp = app_client.patch(
        "/api/session/v1/sessions/ses_1",
        headers=auth_headers,
        json={"task": {
            "instructions": "Summarise the contracts",
            "options": {"hitl": False},
            "attachment_ids": ["att_1"],
        }},
    )

    assert resp.status_code == 200, resp.text
    assert session.agent_metadata["kind"] == "task"
    assert session.agent_metadata["task"] == {
        "instructions": "Summarise the contracts",
        "options": {"hitl": False},
        "attachment_ids": ["att_1"],
    }
    # A fresh dict, not an in-place mutation (JSONB changes must be tracked).
    assert session.agent_metadata is not original_meta


def test_patch_rejects_task_spec_on_a_chat_session(
    app_client: TestClient, mock_db: AsyncMock, auth_headers: dict[str, str],
) -> None:
    session = _session()
    mock_db.execute.return_value = make_scalar_result(session)

    resp = app_client.patch(
        "/api/session/v1/sessions/ses_1",
        headers=auth_headers,
        json={"task": {"instructions": "x"}},
    )

    assert resp.status_code == 422
    assert "task" not in session.agent_metadata


def test_patch_rejects_blank_task_instructions(
    app_client: TestClient, mock_db: AsyncMock, auth_headers: dict[str, str],
) -> None:
    mock_db.execute.return_value = make_scalar_result(_session(kind="task"))
    resp = app_client.patch(
        "/api/session/v1/sessions/ses_1", headers=auth_headers,
        json={"task": {"instructions": ""}},
    )
    assert resp.status_code == 422
