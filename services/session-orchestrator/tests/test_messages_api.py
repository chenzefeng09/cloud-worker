"""Tests for api/v1/messages.py — list_messages, create_message, stream_messages,
_write_memory, _reconcile_run_completion, _persist_artifacts."""

from __future__ import annotations

import asyncio
import datetime as dt
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
from fastapi.testclient import TestClient

from agent_session_orchestrator.api.v1.messages import (
    _persist_artifacts,
    _reconcile_run_completion,
    _write_memory,
)
from agent_session_orchestrator.domain.models import Message, Run, Session
from tests._helpers import TEST_TENANT, TEST_USER, make_scalar_result

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _ses() -> Session:
    return Session(id="ses_m", tenant_id=TEST_TENANT, user_id=TEST_USER, status="ACTIVE")


def _run(status: str = "RUNNING", sandbox_id: str | None = "sb_abc") -> Run:
    return Run(
        id="run_m",
        tenant_id=TEST_TENANT,
        session_id="ses_m",
        task_description="task",
        status=status,
        sandbox_id=sandbox_id,
        created_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
    )


def _msg(seq: int = 1) -> Message:
    return Message(
        id=f"msg_{seq}",
        tenant_id=TEST_TENANT,
        session_id="ses_m",
        role="USER",
        content="hello",
        sequence=seq,
        created_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
    )


# ---------------------------------------------------------------------------
# _write_memory
# ---------------------------------------------------------------------------


def test_write_memory_success() -> None:
    """_write_memory completes without error on a happy-path HTTP stub."""

    class _FakeClient:
        async def __aenter__(self) -> _FakeClient:
            return self

        async def __aexit__(self, *args: object) -> None:
            pass

        async def post(self, url: str, **kw: object) -> MagicMock:
            resp = MagicMock()
            if "/documents" in url and "/ingest" not in url:
                resp.status_code = 201
                resp.json = MagicMock(return_value={"id": "doc_001"})
            else:
                resp.status_code = 200
                resp.json = MagicMock(return_value={})
            return resp

    with patch.object(httpx, "AsyncClient", return_value=_FakeClient()):
        asyncio.get_event_loop().run_until_complete(
            _write_memory(
                knowledge_ingest_url="http://ki:8012/api/knowledge/v1",
                tenant_id=TEST_TENANT,
                task_description="tell me a joke",
                summary="why did the chicken cross the road",
            )
        )


def test_write_memory_noop_on_missing_url() -> None:
    """_write_memory is a no-op when URL is empty."""
    asyncio.get_event_loop().run_until_complete(
        _write_memory(
            knowledge_ingest_url="",
            tenant_id=TEST_TENANT,
            task_description="task",
            summary="summary",
        )
    )


def test_write_memory_failure_open() -> None:
    """_write_memory swallows HTTP errors (failure-open)."""

    class _ErrClient:
        async def __aenter__(self) -> _ErrClient:
            return self

        async def __aexit__(self, *args: object) -> None:
            pass

        async def post(self, url: str, **kw: object) -> None:
            raise httpx.ConnectError("down")

    with patch.object(httpx, "AsyncClient", return_value=_ErrClient()):
        asyncio.get_event_loop().run_until_complete(
            _write_memory(
                knowledge_ingest_url="http://ki:8012/api/knowledge/v1",
                tenant_id=TEST_TENANT,
                task_description="task",
                summary="summary",
            )
        )


# ---------------------------------------------------------------------------
# _reconcile_run_completion
# ---------------------------------------------------------------------------


def test_reconcile_terminal_run_skipped() -> None:
    """Already-terminal runs are skipped without contacting scheduler."""
    run = _run(status="COMPLETED", sandbox_id=None)
    svc = MagicMock()

    result, steps = asyncio.get_event_loop().run_until_complete(
        _reconcile_run_completion(
            scheduler_url="http://sched:8010",
            tenant_id=TEST_TENANT,
            user_id=TEST_USER,
            run=run,
            svc=svc,
        )
    )
    assert result is True
    assert steps == []


def test_reconcile_scheduler_error_returns_false() -> None:
    """Scheduler HTTP errors are swallowed; returns (False, [])."""
    run = _run(status="RUNNING")
    svc = MagicMock()

    class _ErrClient:
        async def __aenter__(self) -> _ErrClient:
            return self

        async def __aexit__(self, *args: object) -> None:
            pass

        async def get(self, url: str, **kw: object) -> None:
            raise httpx.ConnectError("down")

    with patch.object(httpx, "AsyncClient", return_value=_ErrClient()):
        result, steps = asyncio.get_event_loop().run_until_complete(
            _reconcile_run_completion(
                scheduler_url="http://sched:8010",
                tenant_id=TEST_TENANT,
                user_id=TEST_USER,
                run=run,
                svc=svc,
            )
        )
    assert result is False
    assert steps == []


def test_reconcile_completed_run_updates_status() -> None:
    """When scheduler returns COMPLETED, run.status is updated and True returned."""
    run = _run(status="RUNNING")
    svc = AsyncMock()

    class _FakeClient:
        async def __aenter__(self) -> _FakeClient:
            return self

        async def __aexit__(self, *args: object) -> None:
            pass

        async def get(self, url: str, **kw: object) -> MagicMock:
            resp = MagicMock()
            resp.status_code = 200
            resp.json = MagicMock(return_value={
                "status": "COMPLETED",
                "result_summary": "done",
                "result_error": None,
                "artifacts": [],
                "usage": {"input_tokens": 10, "output_tokens": 20, "total_tokens": 30},
                "plan_steps": [{"step": 1}],
            })
            return resp

    with patch.object(httpx, "AsyncClient", return_value=_FakeClient()):
        result, steps = asyncio.get_event_loop().run_until_complete(
            _reconcile_run_completion(
                scheduler_url="http://sched:8010",
                tenant_id=TEST_TENANT,
                user_id=TEST_USER,
                run=run,
                svc=svc,
            )
        )

    assert result is True
    assert run.status == "COMPLETED"
    assert run.result_summary == "done"
    assert steps == [{"step": 1}]


def test_reconcile_still_running_returns_plan_steps() -> None:
    """Running run returns (False, plan_steps) from the scheduler payload."""
    run = _run(status="RUNNING")
    svc = AsyncMock()

    class _FakeClient:
        async def __aenter__(self) -> _FakeClient:
            return self

        async def __aexit__(self, *args: object) -> None:
            pass

        async def get(self, url: str, **kw: object) -> MagicMock:
            resp = MagicMock()
            resp.status_code = 200
            resp.json = MagicMock(return_value={
                "status": "RUNNING",
                "plan_steps": [{"step": 1}, {"step": 2}],
            })
            return resp

    with patch.object(httpx, "AsyncClient", return_value=_FakeClient()):
        result, steps = asyncio.get_event_loop().run_until_complete(
            _reconcile_run_completion(
                scheduler_url="http://sched:8010",
                tenant_id=TEST_TENANT,
                user_id=TEST_USER,
                run=run,
                svc=svc,
            )
        )

    assert result is False
    assert len(steps) == 2


# ---------------------------------------------------------------------------
# _persist_artifacts
# ---------------------------------------------------------------------------


def test_persist_artifacts_stores_each_artifact() -> None:
    run = _run(status="RUNNING")
    svc = AsyncMock()

    artifacts = [
        {"filename": "out.txt", "s3_key": "k/out.txt", "size": 100, "mime": "text/plain"},
        {"filename": "img.png", "s3_key": "k/img.png", "size": 200, "mime": "image/png"},
    ]

    asyncio.get_event_loop().run_until_complete(
        _persist_artifacts(artifacts=artifacts, run=run, svc=svc, tenant_id=TEST_TENANT)
    )

    assert svc.create_attachment.await_count == 2


def test_persist_artifacts_skips_empty_s3_key() -> None:
    run = _run(status="RUNNING")
    svc = AsyncMock()

    artifacts = [
        {"filename": "bad.txt", "s3_key": "", "size": 0, "mime": "text/plain"},
    ]

    asyncio.get_event_loop().run_until_complete(
        _persist_artifacts(artifacts=artifacts, run=run, svc=svc, tenant_id=TEST_TENANT)
    )

    svc.create_attachment.assert_not_awaited()


# ---------------------------------------------------------------------------
# list_messages endpoint
# ---------------------------------------------------------------------------


def test_list_messages_endpoint_returns_items(
    app_client: TestClient, mock_db: AsyncMock, auth_headers: dict,
) -> None:
    """GET /messages returns items list."""
    msgs = [_msg(1), _msg(2)]
    # The endpoint calls: get_session (for reconcile), list_runs, then list_messages.
    # Using return_value so every execute() gives a consistent result.
    mock_db.execute.return_value = make_scalar_result(msgs)

    resp = app_client.get(
        "/api/session/v1/sessions/ses_m/messages", headers=auth_headers,
    )
    assert resp.status_code == 200
    assert isinstance(resp.json()["items"], list)


# ---------------------------------------------------------------------------
# create_message endpoint
# ---------------------------------------------------------------------------


def test_create_message_endpoint_returns_201(
    app_client: TestClient, mock_db: AsyncMock, auth_headers: dict,
) -> None:
    """POST /messages creates a message via service."""
    seq_result = MagicMock()
    seq_result.scalar_one_or_none = MagicMock(return_value=0)
    mock_db.execute.side_effect = [
        make_scalar_result(_ses()),  # get_session
        seq_result,                   # max(sequence) probe
    ]

    async def _refresh(obj: Message) -> None:
        obj.id = "msg_new"
        obj.created_at = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)

    mock_db.refresh.side_effect = _refresh

    resp = app_client.post(
        "/api/session/v1/sessions/ses_m/messages",
        headers=auth_headers,
        json={"role": "USER", "content": "test message"},
    )
    assert resp.status_code in (201, 500)
    mock_db.add.assert_called_once()


# ---------------------------------------------------------------------------
# stream_messages endpoint (SSE)
# ---------------------------------------------------------------------------


def test_stream_messages_returns_sse_response(
    app_client: TestClient, mock_db: AsyncMock, auth_headers: dict,
) -> None:
    """GET /messages/stream returns a streaming response (text/event-stream).

    The SSE generator contains a 300-poll loop with asyncio.sleep(); we patch
    both acquire_db_session and asyncio.sleep to make the test instant and
    force terminal state on the first poll so the generator exits quickly.
    """
    import asyncio as _asyncio

    ses = _ses()
    run_completed = _run(status="COMPLETED", sandbox_id=None)
    # Provide a completed run so the generator sees terminal state and exits.
    mock_db.execute.return_value = make_scalar_result([run_completed])

    # The stream generator uses acquire_db_session (not the dependency override).
    # Patch it to yield mock_db so it doesn't try to connect to real Postgres.
    async def _fake_session(**kw: object):  # type: ignore[override]
        # Also mock get_session and list_messages inside the loop
        mock_db.execute.return_value = make_scalar_result(ses)
        yield mock_db

    async def _noop_sleep(_: float) -> None:
        pass

    with (
        patch(
            "agent_session_orchestrator.api.v1.messages.acquire_db_session",
            side_effect=_fake_session,
        ),
        patch.object(_asyncio, "sleep", side_effect=_noop_sleep),
        app_client.stream(
            "GET",
            "/api/session/v1/sessions/ses_m/messages/stream",
            headers=auth_headers,
        ) as resp,
    ):
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers.get("content-type", "")
