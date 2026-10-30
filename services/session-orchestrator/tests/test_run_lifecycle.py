"""Run lifecycle across the token stream: HITL pause/resume, end markers,
concurrent finalization, cancel, and the one-live-run-per-session guard.

Redis is replaced by an in-memory fake that implements the slice of
``redis.asyncio`` the run-stream code touches (streams + plain keys).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from agent_session_orchestrator.api.v1 import messages as messages_mod
from agent_session_orchestrator.api.v1.messages import (
    _read_ordered_blocks_from_redis,
    _reconcile_run_completion,
    _redis_stream_end_state,
    _stream_tokens,
)
from agent_session_orchestrator.api.v1.runs import (
    _DISPATCH_MAX_BYTES,
    _fit_dispatch_history,
    _json_size,
)
from agent_session_orchestrator.domain.models import Message, Run, Session
from agent_session_orchestrator.services.session_service import SessionService
from tests._helpers import TEST_TENANT, AuthzAwareClient, make_scalar_result
from tests.test_messages_runs import _parse_sse_events, _patch_redis_asyncio


class _RedisState:
    def __init__(self, streams: dict[str, list[dict[str, str]]] | None = None) -> None:
        self.streams: dict[str, list[tuple[str, dict[str, str]]]] = {}
        self.kv: dict[str, str] = {}
        self.ttl: dict[str, int] = {}
        for key, entries in (streams or {}).items():
            for fields in entries:
                self.append(key, fields)

    def append(self, key: str, fields: dict[str, str]) -> str:
        entries = self.streams.setdefault(key, [])
        entry_id = f"{len(entries) + 1}-0"
        entries.append((entry_id, dict(fields)))
        return entry_id


def _fake_redis(state: _RedisState) -> type:
    class _Fake:
        @classmethod
        def from_url(cls, *a: Any, **kw: Any) -> _Fake:
            return cls()

        async def xread(self, streams: dict[str, str], block: int = 0, count: int = 100) -> list:
            (key, last_id), = streams.items()
            after = int(last_id.split("-")[0])
            new = [e for e in state.streams.get(key, []) if int(e[0].split("-")[0]) > after]
            return [(key, new[:count])] if new else []

        async def xrange(self, key: str, min: str = "-", max: str = "+") -> list:
            return list(state.streams.get(key, []))

        async def xadd(self, key: str, fields: dict[str, str]) -> str:
            return state.append(key, fields)

        async def ttl(self, key: str) -> int:
            if key not in state.streams and key not in state.kv:
                return -2
            return state.ttl.get(key, -1)

        async def expire(self, key: str, seconds: int) -> bool:
            state.ttl[key] = seconds
            return True

        async def get(self, key: str) -> str | None:
            return state.kv.get(key)

        async def set(self, key: str, value: str, ex: int | None = None, nx: bool = False) -> bool:
            if nx and key in state.kv:
                return False
            state.kv[key] = value
            if ex:
                state.ttl[key] = ex
            return True

        async def delete(self, *keys: str) -> int:
            n = 0
            for key in keys:
                n += int(state.kv.pop(key, None) is not None)
                n += int(state.streams.pop(key, None) is not None)
            return n

        async def aclose(self) -> None:
            return None

    return _Fake


def _run(status: str = "RUNNING", **kw: Any) -> Run:
    fields: dict[str, Any] = {
        "id": "run_1", "tenant_id": TEST_TENANT, "session_id": "ses_1",
        "task_description": "do the thing", "status": status, "sandbox_id": "sb_old",
        "started_at": dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
        "created_at": dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
    }
    fields.update(kw)
    return Run(**fields)


def _collect(agen: Any) -> list[tuple[str, dict[str, Any]]]:
    async def _drain() -> list[str]:
        return [ev async for ev in agen]

    raw = asyncio.get_event_loop().run_until_complete(_drain())
    return [(name, json.loads(data)) for name, data in _parse_sse_events("".join(raw))]


def _tail(state: _RedisState, *, wait_for_worker: bool, outcome: dict[str, Any]) -> list:
    with _patch_redis_asyncio(_fake_redis(state)):
        deadline = asyncio.get_event_loop().time() + 30
        return _collect(
            _stream_tokens(
                redis_url="redis://fake",
                run_id="run_1",
                deadline=deadline,
                wait_for_worker=wait_for_worker,
                is_run_active=AsyncMock(return_value=True),
                outcome=outcome,
            )
        )


TOKENS = "agent:run:run_1:tokens"


# ── _stream_tokens ───────────────────────────────────────────────────────────


def test_live_tail_stops_at_a_pause_instead_of_waiting_for_the_run() -> None:
    state = _RedisState({TOKENS: [
        {"type": "delta", "text": "Let me check."},
        {"type": "pending_confirmation", "tool_call_id": "tc1", "name": "Bash", "args": "{}"},
    ]})
    outcome: dict[str, Any] = {}

    events = _tail(state, wait_for_worker=True, outcome=outcome)

    names = [n for n, _ in events]
    assert names == ["token", "pending_confirmation", "run_stream_end"]
    assert events[-1][1] == {"run_id": "run_1", "paused": True}
    assert outcome == {"paused": True, "ended": False, "status": ""}


def test_replay_walks_through_a_resolved_pause_to_the_final_end() -> None:
    state = _RedisState({TOKENS: [
        {"type": "delta", "text": "a"},
        {"type": "pending_confirmation", "tool_call_id": "tc1", "name": "Bash", "args": "{}"},
        {"type": "end", "text": "", "status": "paused"},
        {"type": "pending_resolved", "tool_call_id": "tc1"},
        {"type": "delta", "text": "b"},
        {"type": "end", "text": "", "status": "completed"},
        {"type": "delta", "text": "NEVER"},
    ]})
    outcome: dict[str, Any] = {}

    events = _tail(state, wait_for_worker=False, outcome=outcome)

    assert [p["delta"] for n, p in events if n == "token"] == ["a", "b"]
    resolved = [p for n, p in events if n == "pending_resolved"]
    assert resolved == [{"run_id": "run_1", "tool_call_id": "tc1", "offset": "4-0"}]
    assert outcome == {"paused": False, "ended": True, "status": "completed"}
    assert events[-1] == ("run_stream_end", {"run_id": "run_1", "paused": False})


def test_failed_end_marker_is_reported_in_the_outcome() -> None:
    state = _RedisState({TOKENS: [
        {"type": "delta", "text": "partial"},
        {"type": "error", "message": "model overloaded"},
        {"type": "end", "text": "", "status": "failed"},
    ]})
    outcome: dict[str, Any] = {}

    events = _tail(state, wait_for_worker=True, outcome=outcome)

    # Worker error entries are not relayed as stream errors (the client would
    # read them as a broken connection); the failure surfaces via reconcile.
    assert "error" not in [n for n, _ in events]
    assert outcome == {"paused": False, "ended": True, "status": "failed"}


# ── _read_ordered_blocks_from_redis ──────────────────────────────────────────


def _blocks(entries: list[dict[str, str]]) -> list[dict[str, Any]]:
    state = _RedisState({TOKENS: entries})
    with _patch_redis_asyncio(_fake_redis(state)):
        return asyncio.get_event_loop().run_until_complete(
            _read_ordered_blocks_from_redis(redis_url="redis://fake", run_id="run_1")
        )


def test_a_tool_call_reported_twice_becomes_one_block_with_the_parseable_args() -> None:
    blocks = _blocks([
        {"type": "tool_call", "tool_call_id": "tc1", "name": "Read",
         "args": '{"path": "a"}{"path": "a"}'},
        {"type": "tool_call", "tool_call_id": "tc1", "name": "Read", "args": '{"path": "a"}'},
        {"type": "tool_result", "tool_call_id": "tc1", "name": "Read", "result": "x",
         "state": "success"},
    ])

    assert blocks == [{
        "kind": "tool", "tool_call_id": "tc1", "name": "Read", "input": {"path": "a"},
        "result": "x", "state": "success",
    }]


def test_streamed_thinking_after_text_keeps_the_true_order() -> None:
    blocks = _blocks([
        {"type": "delta", "text": "intro"},
        {"type": "thinking_delta", "text": "hmm"},
        {"type": "delta", "text": "answer"},
        {"type": "end"},
    ])

    assert blocks == [
        {"kind": "text", "text": "intro"},
        {"kind": "thinking", "text": "hmm"},
        {"kind": "text", "text": "answer"},
    ]


def test_both_legs_of_a_resumed_run_are_replayed() -> None:
    blocks = _blocks([
        {"type": "delta", "text": "before"},
        {"type": "pending_question", "tool_call_id": "q1", "question": "Which file?"},
        {"type": "end", "text": "", "status": "paused"},
        {"type": "pending_resolved", "tool_call_id": "q1"},
        {"type": "delta", "text": "after"},
        {"type": "end", "text": "", "status": "completed"},
    ])

    assert blocks == [{"kind": "text", "text": "before"}, {"kind": "text", "text": "after"}]


# ── _redis_stream_end_state ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("entries", "expected"),
    [
        ([{"type": "delta", "text": "x"}], (None, None)),
        ([{"type": "pending_confirmation", "tool_call_id": "tc1"}], ("paused", None)),
        (
            [{"type": "end", "status": "paused"}, {"type": "pending_resolved", "tool_call_id": ""},
             {"type": "delta", "text": "resumed"}],
            (None, None),
        ),
        ([{"type": "error", "message": "boom"}, {"type": "end", "status": "failed"}],
         ("failed", "boom")),
        ([{"type": "end", "text": ""}], ("", None)),
    ],
    ids=["no-end", "unanswered-prompt", "resumed-leg-running", "failed", "legacy-end"],
)
def test_end_state_reflects_the_latest_leg(
    entries: list[dict[str, str]], expected: tuple[str | None, str | None],
) -> None:
    state = _RedisState({TOKENS: entries})
    with _patch_redis_asyncio(_fake_redis(state)):
        result = asyncio.get_event_loop().run_until_complete(
            _redis_stream_end_state(redis_url="redis://fake", run_id="run_1")
        )
    assert result == expected


def test_heartbeat_entries_change_nothing_for_any_stream_reader() -> None:
    """The worker's keep-alive entries (written while a long tool call is silent)
    must stay invisible: no SSE event, no persisted block, no effect on the end
    state."""
    entries = [
        {"type": "delta", "text": "Running the import."},
        {"type": "tool_call", "tool_call_id": "tc1", "name": "mcp__etl__run", "args": "{}"},
        {"type": "heartbeat"},
        {"type": "heartbeat"},
        {"type": "tool_result", "tool_call_id": "tc1", "name": "mcp__etl__run",
         "result": "ok", "state": "success"},
        {"type": "delta", "text": "Done."},
        {"type": "end", "text": "", "status": "completed"},
    ]
    without = [e for e in entries if e["type"] != "heartbeat"]

    def _relayed(stream: list[dict[str, str]]) -> tuple[list[Any], dict[str, Any]]:
        outcome: dict[str, Any] = {}
        events = _tail(_RedisState({TOKENS: stream}), wait_for_worker=False, outcome=outcome)
        return [(n, {k: v for k, v in p.items() if k != "offset"}) for n, p in events], outcome

    def _end_state(stream: list[dict[str, str]]) -> tuple[str | None, str | None]:
        with _patch_redis_asyncio(_fake_redis(_RedisState({TOKENS: stream}))):
            return asyncio.get_event_loop().run_until_complete(
                _redis_stream_end_state(redis_url="redis://fake", run_id="run_1")
            )

    assert _relayed(entries) == _relayed(without)
    assert _blocks(entries) == _blocks(without)
    assert _end_state(entries) == _end_state(without) == ("completed", None)


# ── _reconcile_run_completion ────────────────────────────────────────────────


def _scheduler(status_code: int, payload: dict[str, Any] | None = None) -> Any:
    class _Resp:
        def __init__(self) -> None:
            self.status_code = status_code

        def json(self) -> dict[str, Any]:
            return payload or {}

    class _Client:
        async def __aenter__(self) -> _Client:
            return self

        async def __aexit__(self, *a: Any) -> None:
            return None

        async def get(self, url: str, **kw: Any) -> _Resp:
            return _Resp()

    return patch.object(httpx, "AsyncClient", return_value=_Client())


def _reconcile_svc(*, claim: bool = True) -> MagicMock:
    svc = MagicMock()
    svc.claim_run_transition = AsyncMock(return_value=claim)
    svc.run_has_output = AsyncMock(return_value=False)
    svc.create_message = AsyncMock(return_value=SimpleNamespace(id="msg_x"))
    return svc


def _reconcile(run: Run, svc: MagicMock, state: _RedisState) -> tuple[bool, list[Any]]:
    with _patch_redis_asyncio(_fake_redis(state)):
        return asyncio.get_event_loop().run_until_complete(
            _reconcile_run_completion(
                scheduler_url="http://scheduler", tenant_id=TEST_TENANT, user_id="u1",
                run=run, svc=svc, redis_url="redis://fake",
            )
        )


def test_a_reconciler_that_loses_the_claim_persists_nothing() -> None:
    run = _run()
    svc = _reconcile_svc(claim=False)

    async def _lost(r: Run, **kw: Any) -> bool:
        r.status = "COMPLETED"  # what the winner committed (claim refreshes the row)
        return False

    svc.claim_run_transition = AsyncMock(side_effect=_lost)
    state = _RedisState({TOKENS: [{"type": "delta", "text": "hi"}, {"type": "end"}]})

    with _scheduler(200, {"status": "COMPLETED", "result_summary": "hi"}):
        terminal, _ = _reconcile(run, svc, state)

    assert terminal is True
    svc.create_message.assert_not_called()


def test_a_scheduler_failure_leaves_a_note_in_the_transcript() -> None:
    run = _run()
    svc = _reconcile_svc()
    state = _RedisState({TOKENS: [{"type": "delta", "text": "partial"}]})
    state.kv["agent:run:run_1:dispatch"] = "{}"

    with _scheduler(200, {"status": "FAILED", "result_error": "Rate limited\nTraceback ..."}):
        terminal, _ = _reconcile(run, svc, state)

    assert terminal is True
    assert run.status == "FAILED"
    rows = [(c.kwargs["role"], c.kwargs["content"]) for c in svc.create_message.call_args_list]
    assert rows == [("assistant", "partial"), ("system", "The run failed: Rate limited")]
    # The resume payload is dropped once the run can no longer be resumed.
    assert "agent:run:run_1:dispatch" not in state.kv


def test_sandbox_gone_while_waiting_for_input_marks_the_run_paused() -> None:
    run = _run()
    svc = _reconcile_svc()
    state = _RedisState({TOKENS: [
        {"type": "pending_question", "tool_call_id": "q1", "question": "Which?"},
        {"type": "end", "text": "", "status": "paused"},
    ]})

    with _scheduler(404):
        terminal, _ = _reconcile(run, svc, state)

    assert terminal is False
    assert run.status == "PAUSED"
    svc.create_message.assert_not_called()


def test_sandbox_gone_after_a_failed_end_marks_the_run_failed_with_the_worker_error() -> None:
    run = _run()
    svc = _reconcile_svc()
    state = _RedisState({TOKENS: [
        {"type": "delta", "text": "partial"},
        {"type": "error", "message": "tool crashed"},
        {"type": "end", "text": "", "status": "failed"},
    ]})

    with _scheduler(404):
        terminal, _ = _reconcile(run, svc, state)

    assert terminal is True
    assert run.status == "FAILED"
    assert run.error_message == "tool crashed"
    rows = [(c.kwargs["role"], c.kwargs["content"]) for c in svc.create_message.call_args_list]
    assert rows == [("assistant", "partial"), ("system", "The run failed: tool crashed")]


# ── SessionService.claim_run_transition ──────────────────────────────────────


def test_claim_wins_when_the_conditional_update_matches() -> None:
    db = AsyncMock()
    db.execute = AsyncMock(return_value=MagicMock(rowcount=1))
    run = _run()

    won = asyncio.get_event_loop().run_until_complete(
        SessionService(db, tenant_id=TEST_TENANT).claim_run_transition(run, to_status="COMPLETED")
    )

    assert won is True
    assert run.status == "COMPLETED"
    sql = str(db.execute.call_args.args[0])
    assert "run.status IN" in sql and "run.sandbox_id =" in sql
    db.refresh.assert_not_called()


def test_claim_loses_and_reloads_the_row_when_someone_else_moved_it() -> None:
    db = AsyncMock()
    db.execute = AsyncMock(return_value=MagicMock(rowcount=0))
    run = _run()

    async def _reload(obj: Run) -> None:
        obj.status = "CANCELLED"

    db.refresh = AsyncMock(side_effect=_reload)

    won = asyncio.get_event_loop().run_until_complete(
        SessionService(db, tenant_id=TEST_TENANT).claim_run_transition(run, to_status="COMPLETED")
    )

    assert won is False
    assert run.status == "CANCELLED"


def test_a_cancel_claim_ignores_the_sandbox() -> None:
    db = AsyncMock()
    db.execute = AsyncMock(return_value=MagicMock(rowcount=1))

    asyncio.get_event_loop().run_until_complete(
        SessionService(db, tenant_id=TEST_TENANT).claim_run_transition(
            _run(), to_status="CANCELLED", from_statuses=("RUNNING", "PAUSED"),
            match_sandbox=False,
        )
    )

    assert "sandbox_id" not in str(db.execute.call_args.args[0])


# ── endpoints ────────────────────────────────────────────────────────────────


def _session() -> Session:
    return Session(id="ses_1", tenant_id=TEST_TENANT, user_id="u1", status="ACTIVE")


def test_create_run_refuses_a_second_live_run(
    app_client: TestClient, mock_db: AsyncMock, auth_headers: dict[str, str],
) -> None:
    mock_db.execute.side_effect = [
        make_scalar_result(_session()),   # get_session
        make_scalar_result([_run()]),     # list_runs → a RUNNING run
    ]
    dispatched: list[Any] = []

    class _Client(AuthzAwareClient):
        async def post(self, url: str, **kw: Any) -> httpx.Response:
            if "/check" in url and "authz" in url:
                return self._authz_allowed_resp()
            dispatched.append(url)
            return httpx.Response(201, json={"sandbox_id": "sb_new"})

    with patch.object(httpx, "AsyncClient", return_value=_Client()):
        resp = app_client.post(
            "/api/session/v1/sessions/ses_1/runs", headers=auth_headers, json={"task": "again"},
        )

    assert resp.status_code == 409
    assert "already in progress" in resp.text
    assert dispatched == []


def test_cancelling_a_finished_run_changes_nothing(
    app_client: TestClient, mock_db: AsyncMock, auth_headers: dict[str, str],
) -> None:
    mock_db.execute.side_effect = [make_scalar_result(_run(status="COMPLETED"))]
    calls: list[str] = []

    class _Client(AuthzAwareClient):
        async def post(self, url: str, **kw: Any) -> httpx.Response:
            if "/check" in url and "authz" in url:
                return self._authz_allowed_resp()
            calls.append(url)
            return httpx.Response(200, json={})

    with patch.object(httpx, "AsyncClient", return_value=_Client()):
        resp = app_client.post(
            "/api/session/v1/sessions/ses_1/runs/run_1/cancel", headers=auth_headers,
        )

    assert resp.status_code == 200
    assert resp.json() == {"id": "run_1", "status": "COMPLETED"}
    assert calls == []
    mock_db.add.assert_not_called()


def test_cancel_keeps_the_partial_output_and_closes_the_stream(
    app_client: TestClient, mock_db: AsyncMock, auth_headers: dict[str, str],
) -> None:
    app_client.app.state.settings.redis_url = "redis://fake"
    state = _RedisState({TOKENS: [{"type": "delta", "text": "half an answer"}]})
    state.kv["agent:run:run_1:paused"] = "{}"
    state.kv["agent:run:run_1:dispatch"] = "{}"

    seq = MagicMock()
    seq.scalar_one_or_none = MagicMock(return_value=0)
    no_output = MagicMock()
    no_output.first = MagicMock(return_value=None)
    mock_db.execute.side_effect = [
        make_scalar_result(_run(status="PAUSED")),  # get_run
        MagicMock(rowcount=1),                      # claim CANCELLED
        no_output,                                  # run_has_output
        make_scalar_result(_session()), seq,        # create_message (assistant text)
        make_scalar_result(_session()), seq,        # create_message ("Task cancelled")
    ]
    added: list[Message] = []
    mock_db.add = MagicMock(side_effect=added.append)

    async def _refresh(obj: Any, attribute_names: list[str] | None = None) -> None:
        if isinstance(obj, Message):
            obj.id = f"msg_{len(added)}"
            obj.created_at = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)

    mock_db.refresh.side_effect = _refresh

    class _Client(AuthzAwareClient):
        async def post(self, url: str, **kw: Any) -> httpx.Response:
            if "/check" in url and "authz" in url:
                return self._authz_allowed_resp()
            return httpx.Response(200, json={})

    with (
        patch.object(httpx, "AsyncClient", return_value=_Client()),
        _patch_redis_asyncio(_fake_redis(state)),
    ):
        resp = app_client.post(
            "/api/session/v1/sessions/ses_1/runs/run_1/cancel", headers=auth_headers,
        )

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"id": "run_1", "status": "CANCELLED"}
    assert [(m.role, m.content) for m in added] == [
        ("assistant", "half an answer"),
        ("system", "Task cancelled"),
    ]
    assert state.streams[TOKENS][-1][1] == {"type": "end", "text": "", "status": "cancelled"}
    assert "agent:run:run_1:paused" not in state.kv
    assert "agent:run:run_1:dispatch" not in state.kv


def test_resume_replays_the_original_dispatch_and_settles_every_prompt(
    app_client: TestClient, mock_db: AsyncMock, auth_headers: dict[str, str],
) -> None:
    app_client.app.state.settings.redis_url = "redis://fake"
    state = _RedisState({TOKENS: [
        {"type": "pending_confirmation", "tool_call_id": "tc1", "name": "Bash", "args": "{}"},
        {"type": "pending_confirmation", "tool_call_id": "tc2", "name": "Bash", "args": "{}"},
    ]})
    state.kv["agent:run:run_1:paused"] = json.dumps({
        "kind": "confirm",
        "tool_calls": [{"id": "tc1", "name": "Bash"}, {"id": "tc2", "name": "Bash"}],
        "agent_state": {"transcript": []},
    })
    original = {
        "session_id": "ses_1", "run_id": "run_1", "task": "do the thing",
        "agent_config": {"model": "claude-opus-5-5"}, "skills": ["poet"], "hitl": True,
        "history": [{"role": "user", "content": "earlier"}], "sys_prompt": "Be brief.",
        "mcp_server_ids": [],
    }
    state.kv["agent:run:run_1:dispatch"] = json.dumps(original)
    mock_db.execute.side_effect = [
        make_scalar_result(_run(status="PAUSED")),  # get_run
        MagicMock(rowcount=1),                      # claim RUNNING for the resume
    ]
    dispatched: list[dict[str, Any]] = []

    class _Client(AuthzAwareClient):
        async def post(self, url: str, **kw: Any) -> httpx.Response:
            if "/check" in url and "authz" in url:
                return self._authz_allowed_resp()
            dispatched.append(kw["json"])
            return httpx.Response(201, json={"sandbox_id": "sb_resumed_0000"})

    with (
        patch.object(httpx, "AsyncClient", return_value=_Client()),
        _patch_redis_asyncio(_fake_redis(state)),
    ):
        resp = app_client.post(
            "/api/session/v1/sessions/ses_1/runs/run_1/respond",
            headers=auth_headers,
            json={"kind": "confirm", "tool_call_id": "tc1", "confirmed": True},
        )

    assert resp.status_code == 200, resp.text
    (body,) = dispatched
    assert {k: body[k] for k in original} == original
    assert body["resume"]["tool_call_id"] == "tc1"
    assert body["resume"]["agent_state"] == {"transcript": []}
    tail = [fields for _, fields in state.streams[TOKENS][2:]]
    assert tail == [
        {"type": "pending_resolved", "tool_call_id": "tc1"},
        {"type": "pending_resolved", "tool_call_id": "tc2"},
    ]
    assert "agent:run:run_1:paused" not in state.kv


def test_resume_is_refused_when_the_run_was_cancelled_meanwhile(
    app_client: TestClient, mock_db: AsyncMock, auth_headers: dict[str, str],
) -> None:
    app_client.app.state.settings.redis_url = "redis://fake"
    state = _RedisState()
    state.kv["agent:run:run_1:paused"] = json.dumps({"kind": "confirm", "tool_calls": []})
    run = _run(status="PAUSED")
    mock_db.execute.side_effect = [make_scalar_result(run), MagicMock(rowcount=0)]

    async def _reload(obj: Run, attribute_names: list[str] | None = None) -> None:
        obj.status = "CANCELLED"

    mock_db.refresh.side_effect = _reload
    dispatched: list[Any] = []

    class _Client(AuthzAwareClient):
        async def post(self, url: str, **kw: Any) -> httpx.Response:
            if "/check" in url and "authz" in url:
                return self._authz_allowed_resp()
            dispatched.append(url)
            return httpx.Response(201, json={"sandbox_id": "sb_x"})

    with (
        patch.object(httpx, "AsyncClient", return_value=_Client()),
        _patch_redis_asyncio(_fake_redis(state)),
    ):
        resp = app_client.post(
            "/api/session/v1/sessions/ses_1/runs/run_1/respond",
            headers=auth_headers,
            json={"kind": "confirm", "tool_call_id": "tc1", "confirmed": True},
        )

    assert resp.status_code == 409
    assert "CANCELLED" in resp.text
    assert dispatched == []
    assert "agent:run:run_1:resume_lock" not in state.kv


# ── dispatch size budget ─────────────────────────────────────────────────────


def _turns(n: int, size: int = 1000) -> list[dict[str, Any]]:
    """``n`` history items, oldest first, each carrying ``size`` filler characters."""
    return [
        {"role": "user" if i % 2 == 0 else "assistant", "type": "text",
         "content": f"turn {i}: " + "x" * size}
        for i in range(n)
    ]


def test_a_dispatch_within_the_budget_is_sent_unchanged() -> None:
    payload = {"task": "t", "history": _turns(10)}
    before = json.loads(json.dumps(payload))
    _fit_dispatch_history(payload, run_id="run_1")
    assert payload == before


def test_an_oversized_dispatch_drops_the_oldest_history_behind_a_marker() -> None:
    history = _turns(300)
    payload = {"task": "t", "memory": "m", "history": list(history)}
    _fit_dispatch_history(payload, run_id="run_1")

    marker, *kept = payload["history"]
    dropped = len(history) - len(kept)
    assert marker["role"] == "system"
    assert marker["type"] == "history_omitted"
    assert marker["omitted"] == dropped > 0
    assert f"{dropped} earlier message(s) omitted" in marker["content"]
    assert kept == history[dropped:]  # the newest turns, in order
    assert _json_size(payload) <= _DISPATCH_MAX_BYTES
    # As much as fits is kept: one more turn would not fit.
    payload["history"] = [marker, history[dropped - 1], *kept]
    assert _json_size(payload) > _DISPATCH_MAX_BYTES


def test_a_second_fit_merges_the_earlier_marker_instead_of_stacking_one() -> None:
    history = _turns(300)
    payload = {"task": "t", "history": list(history)}
    _fit_dispatch_history(payload, run_id="run_1")
    first_dropped = payload["history"][0]["omitted"]

    payload["resume"] = {"kind": "confirm", "agent_state": {"transcript": "y" * 30_000}}
    _fit_dispatch_history(payload, run_id="run_1")

    marker, *kept = payload["history"]
    assert [h["type"] for h in payload["history"]].count("history_omitted") == 1
    assert marker["omitted"] == len(history) - len(kept) > first_dropped
    assert kept == history[marker["omitted"]:]
    assert _json_size(payload) <= _DISPATCH_MAX_BYTES


def test_a_dispatch_too_big_even_without_history_is_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    payload = {"task": "t", "memory": "z" * _DISPATCH_MAX_BYTES, "history": _turns(3)}
    with caplog.at_level(logging.WARNING, logger="agent_session_orchestrator.api.v1.runs"):
        _fit_dispatch_history(payload, run_id="run_1")
    (marker,) = payload["history"]
    assert marker["omitted"] == 3
    assert "no history left" in caplog.text


def _scheduler_capture(dispatched: list[dict[str, Any]]) -> AuthzAwareClient:
    class _Client(AuthzAwareClient):
        async def post(self, url: str, **kw: Any) -> httpx.Response:
            if "/check" in url and "authz" in url:
                return self._authz_allowed_resp()
            if url.endswith("/runs"):
                dispatched.append(kw["json"])
            return httpx.Response(201, json={"sandbox_id": "sb_new_000000"})

    return _Client()


def test_create_run_fits_a_long_conversation_into_the_dispatch(
    app_client: TestClient, mock_db: AsyncMock, auth_headers: dict[str, str],
) -> None:
    mock_db.execute.return_value = make_scalar_result(_session())

    async def _refresh(obj: Any, attribute_names: list[str] | None = None) -> None:
        if isinstance(obj, Run):
            obj.id = "run_1"
            obj.created_at = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)

    mock_db.refresh.side_effect = _refresh
    dispatched: list[dict[str, Any]] = []
    history = _turns(300)

    with (
        patch.object(httpx, "AsyncClient", return_value=_scheduler_capture(dispatched)),
        patch(
            "agent_session_orchestrator.api.v1.runs._fetch_memory",
            new=AsyncMock(return_value=""),
        ),
        patch(
            "agent_session_orchestrator.api.v1.runs._assemble_history",
            new=AsyncMock(return_value=history),
        ),
        patch(
            "agent_session_orchestrator.api.v1.runs._fetch_default_installed_skills",
            new=AsyncMock(return_value=[]),
        ),
    ):
        resp = app_client.post(
            "/api/session/v1/sessions/ses_1/runs", headers=auth_headers, json={"task": "next"},
        )

    assert resp.status_code == 201, resp.text
    (body,) = dispatched
    assert _json_size(body) <= _DISPATCH_MAX_BYTES
    marker, *kept = body["history"]
    assert marker["type"] == "history_omitted"
    assert kept == history[marker["omitted"]:]


def test_resume_drops_more_history_to_make_room_for_the_resume_payload(
    app_client: TestClient, mock_db: AsyncMock, auth_headers: dict[str, str],
) -> None:
    app_client.app.state.settings.redis_url = "redis://fake"
    state = _RedisState()
    agent_state = {
        "original_task": "do the thing",
        "transcript": [{"role": "assistant", "text": "y" * 40_000}],
        "pending": [{"id": "tc1", "name": "mcp__etl__run", "args": "{}"}],
    }
    state.kv["agent:run:run_1:paused"] = json.dumps({
        "kind": "confirm",
        "tool_calls": [{"id": "tc1", "name": "mcp__etl__run"}],
        "agent_state": agent_state,
    })
    history = _turns(100)
    original = {
        "session_id": "ses_1", "run_id": "run_1", "task": "do the thing", "hitl": True,
        "history": history,
    }
    assert _json_size(original) <= _DISPATCH_MAX_BYTES  # the first leg fitted
    state.kv["agent:run:run_1:dispatch"] = json.dumps(original)
    mock_db.execute.side_effect = [
        make_scalar_result(_run(status="PAUSED")),  # get_run
        MagicMock(rowcount=1),                      # claim RUNNING for the resume
    ]
    dispatched: list[dict[str, Any]] = []

    with (
        patch.object(httpx, "AsyncClient", return_value=_scheduler_capture(dispatched)),
        _patch_redis_asyncio(_fake_redis(state)),
    ):
        resp = app_client.post(
            "/api/session/v1/sessions/ses_1/runs/run_1/respond",
            headers=auth_headers,
            json={"kind": "confirm", "tool_call_id": "tc1", "confirmed": True},
        )

    assert resp.status_code == 200, resp.text
    (body,) = dispatched
    assert _json_size(body) <= _DISPATCH_MAX_BYTES
    assert body["resume"]["agent_state"] == agent_state
    marker, *kept = body["history"]
    assert marker["type"] == "history_omitted"
    assert marker["omitted"] == len(history) - len(kept) > 0
    assert kept == history[marker["omitted"]:]


def test_pending_ignores_blobs_left_by_finished_runs(
    app_client: TestClient, mock_db: AsyncMock, auth_headers: dict[str, str],
) -> None:
    app_client.app.state.settings.redis_url = "redis://fake"
    state = _RedisState()
    blob = json.dumps({"kind": "question", "question": "Which?", "tool_calls": []})
    state.kv["agent:run:run_done:paused"] = blob
    state.kv["agent:run:run_live:paused"] = blob
    mock_db.execute.side_effect = [make_scalar_result([
        _run(id="run_done", status="COMPLETED"),
        _run(id="run_live", status="PAUSED"),
    ])]

    with _patch_redis_asyncio(_fake_redis(state)):
        resp = app_client.get("/api/session/v1/sessions/ses_1/pending", headers=auth_headers)

    assert resp.status_code == 200
    assert [item["run_id"] for item in resp.json()["items"]] == ["run_live"]


# ── stream_session ───────────────────────────────────────────────────────────


def test_a_finished_stream_is_not_retailed_while_the_row_lags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The worker's ``end`` is read while the scheduler still reports RUNNING:
    the run must not be re-tailed (no second run_started); run_complete follows
    once a later poll's reconcile lands the terminal status."""
    run = _run()
    polls = {"n": 0}

    async def _fake_acquire(*, tenant_id: str, **_kw: Any) -> Any:
        yield object()

    class _Svc:
        def __init__(self, *a: Any, **kw: Any) -> None:
            pass

        async def get_session(self, session_id: str) -> Any:
            return SimpleNamespace(user_id="u1", title="t")

        async def list_runs(self, session_id: str) -> list[Run]:
            return [run]

        async def list_messages(self, session_id: str, cursor: Any = None, limit: int = 50) -> Any:
            return [], None

    async def _reconcile(*, run: Run, **kw: Any) -> tuple[bool, list[Any]]:
        polls["n"] += 1
        if polls["n"] >= 4:  # the container is finally reported finished
            run.status = "COMPLETED"
            return True, []
        return False, []

    async def _tokens(**kw: Any) -> Any:
        kw["outcome"].update(paused=False, ended=True, status="completed")
        yield messages_mod._sse("token", {"run_id": "run_1", "delta": "hi", "offset": "1-0"})
        yield messages_mod._sse("run_stream_end", {"run_id": "run_1", "paused": False})

    async def _materialize(**kw: Any) -> list[str]:
        return []

    monkeypatch.setattr(messages_mod, "acquire_db_session", _fake_acquire)
    monkeypatch.setattr(messages_mod, "SessionService", _Svc)
    monkeypatch.setattr(messages_mod, "_reconcile_run_completion", _reconcile)
    monkeypatch.setattr(messages_mod, "_stream_tokens", _tokens)
    monkeypatch.setattr(messages_mod, "_materialize_pending_canvases", _materialize)
    monkeypatch.setattr(messages_mod.asyncio, "sleep", AsyncMock(return_value=None))

    settings = SimpleNamespace(
        agent_scheduler_url="http://s", knowledge_ingest_url="", foundation_dlp_url="",
        redis_url="redis://fake",
    )
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(settings=settings)))

    async def _run_stream() -> list[str]:
        response = await messages_mod.stream_session(
            session_id="ses_1", request=request, tenant_id=TEST_TENANT,  # type: ignore[arg-type]
        )
        out: list[str] = []
        agen = response.body_iterator
        try:
            while True:
                chunk = await agen.__anext__()
                out.append(chunk if isinstance(chunk, str) else chunk.decode())
                if "event: run_complete" in out[-1]:
                    break
        finally:
            await agen.aclose()
        return out

    events = _parse_sse_events("".join(asyncio.get_event_loop().run_until_complete(_run_stream())))
    names = [n for n, _ in events]
    assert names.count("run_started") == 1
    assert names.count("token") == 1
    assert names[-1] == "run_complete"
    assert json.loads(events[-1][1])["status"] == "COMPLETED"


# ── rewind detection ─────────────────────────────────────────────────────────


def _msg(seq: int, msg_id: str, content: str = "") -> Message:
    return Message(
        id=msg_id, tenant_id=TEST_TENANT, session_id="ses_1", role="user",
        content=content or msg_id, sequence=seq,
        created_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
    )


class _TranscriptSvc:
    """SessionService stand-in whose ``list_messages`` honours the cursor."""

    rows: list[Message] = []

    def __init__(self, *a: Any, **kw: Any) -> None:
        pass

    async def get_session(self, session_id: str) -> Any:
        return SimpleNamespace(user_id="u1", title="t")

    async def list_runs(self, session_id: str) -> list[Run]:
        return []

    async def list_messages(
        self, session_id: str, cursor: str | None = None, limit: int = 50,
    ) -> tuple[list[Message], str | None]:
        after = int(cursor) if cursor else 0
        rows = [m for m in sorted(self.rows, key=lambda m: m.sequence) if m.sequence > after]
        if len(rows) > limit:
            return rows[:limit], str(rows[limit - 1].sequence)
        return rows, None

    async def latest_message(self, session_id: str) -> Message | None:
        return max(self.rows, key=lambda m: m.sequence, default=None)


def _messages_after(rows: list[Message], after: int, anchor: str | None) -> list[str] | None:
    svc = _TranscriptSvc()
    svc.rows = rows
    result = asyncio.get_event_loop().run_until_complete(
        messages_mod._messages_after(svc, "ses_1", after, anchor)  # type: ignore[arg-type]
    )
    return None if result is None else [m.id for m in result]


def test_rows_after_an_anchor_that_is_still_there() -> None:
    rows = [_msg(1, "m1"), _msg(2, "m2"), _msg(3, "m3")]

    assert _messages_after(rows, 2, "m2") == ["m3"]
    assert _messages_after(rows, 2, None) == ["m3"]
    assert _messages_after(rows, 0, None) == ["m1", "m2", "m3"]


@pytest.mark.parametrize(
    "rows",
    [
        [_msg(1, "m1")],                                # truncated below the cursor
        [_msg(1, "m1"), _msg(2, "m2b"), _msg(3, "m3b")],  # sequence reused
    ],
    ids=["truncated", "sequence-reused"],
)
def test_a_vanished_anchor_reports_a_rewind(rows: list[Message]) -> None:
    assert _messages_after(rows, 2, "m2") is None


def test_the_stream_resets_the_client_after_a_rewind_and_carries_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Another tab rewinds to sequence 2 and resends: this connection (holding
    rows up to m3) is told to reload, then streams the new rows."""
    polls = {"n": 0}
    svc_rows = [_msg(1, "m1"), _msg(2, "m2"), _msg(3, "m3")]

    class _Svc(_TranscriptSvc):
        rows = svc_rows

        async def list_runs(self, session_id: str) -> list[Run]:
            polls["n"] += 1
            if polls["n"] == 2:  # rewind + the edited prompt
                svc_rows[1:] = [_msg(2, "m2b", "edited")]
            if polls["n"] == 3:  # the next row after the reset
                svc_rows.append(_msg(3, "m3b", "reply"))
            return []

    async def _fake_acquire(*, tenant_id: str, **_kw: Any) -> Any:
        yield object()

    monkeypatch.setattr(messages_mod, "acquire_db_session", _fake_acquire)
    monkeypatch.setattr(messages_mod, "SessionService", _Svc)
    monkeypatch.setattr(messages_mod.asyncio, "sleep", AsyncMock(return_value=None))
    settings = SimpleNamespace(
        agent_scheduler_url="http://s", knowledge_ingest_url="", foundation_dlp_url="",
        redis_url=None,
    )
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(settings=settings)))

    async def _run_stream() -> list[str]:
        response = await messages_mod.stream_session(
            session_id="ses_1", request=request, tenant_id=TEST_TENANT,  # type: ignore[arg-type]
            after=3, after_id="m3",
        )
        out: list[str] = []
        agen = response.body_iterator
        try:
            while polls["n"] < 4:
                chunk = await agen.__anext__()
                out.append(chunk if isinstance(chunk, str) else chunk.decode())
        finally:
            await agen.aclose()
        return out

    events = _parse_sse_events("".join(asyncio.get_event_loop().run_until_complete(_run_stream())))
    lifecycle = [
        (name, json.loads(data).get("id")) for name, data in events
        if name in ("message", "transcript_reset")
    ]
    # Nothing is resent on connect; the rewind is reported once; the row
    # after the new head arrives normally.
    assert lifecycle == [("transcript_reset", None), ("message", "m3b")]
