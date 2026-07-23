"""Coverage for chat-mode inline runs (execution_mode='inline').

Inline runs never touch the scheduler: the orchestrator streams a plain chat
completion from the LLM gateway in-process into the same Redis token stream the
sandbox worker uses.  These tests exercise:

* the streaming wire mapping (delta→delta entry, reasoning_content→single
  thinking entry, usage frame, [DONE], error frame);
* the executor's full lifecycle (COMPLETED with tokens + persisted messages,
  idempotent replay, FAILED-without-assistant-error-message on gateway errors);
* the create_run inline branch (RUNNING before response, sandbox_id null, no
  scheduler call);
* cancel;
* the sweeper's inline-orphan branch.

Style mirrors ``test_messages_runs.py``: fake httpx clients, an injected fake
``redis.asyncio`` module, and direct calls to the module-private helpers.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import sys
import types
import unittest.mock as mock
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from agent_session_orchestrator.domain.models import Run, Session
from tests._helpers import TEST_TENANT, TEST_USER, AuthzAwareClient, make_scalar_result

# ─────────────────────────────────────────────────────────────────────────────
# Fakes
# ─────────────────────────────────────────────────────────────────────────────


class _FakeRedisStream:
    """In-memory stand-in for a run token stream (XADD + XRANGE).

    Shared across a test via ``from_url`` returning the same singleton so the
    executor's writes are visible to a later replay/read.
    """

    _instance: _FakeRedisStream | None = None

    def __init__(self) -> None:
        self.entries: list[tuple[str, dict[str, Any]]] = []
        self._seq = 0

    @classmethod
    def reset(cls) -> None:
        cls._instance = None

    @classmethod
    def from_url(cls, *a: Any, **kw: Any) -> _FakeRedisStream:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    async def xadd(self, key: str, entry: dict[str, Any]) -> str:
        self._seq += 1
        eid = f"{self._seq}-0"
        # Redis coerces everything to strings; mirror that.
        self.entries.append((eid, {str(k): str(v) for k, v in entry.items()}))
        return eid

    async def xrange(self, key: str, min: str = "-", max: str = "+") -> list:
        return list(self.entries)

    async def aclose(self) -> None:
        return None


def _patch_redis(fake_cls: type) -> Any:
    """Inject a fake ``redis.asyncio`` module (same trick as test_messages_runs)."""
    fake_mod = types.ModuleType("redis.asyncio")
    fake_mod.Redis = fake_cls  # type: ignore[attr-defined]
    if "redis" in sys.modules or "redis.asyncio" in sys.modules:
        real = sys.modules.get("redis.asyncio") or sys.modules["redis"].asyncio
        return mock.patch.object(real, "Redis", new=fake_cls)
    fake_pkg = types.ModuleType("redis")
    fake_pkg.asyncio = fake_mod  # type: ignore[attr-defined]
    return mock.patch.dict(sys.modules, {"redis": fake_pkg, "redis.asyncio": fake_mod})


class _FakeStreamResponse:
    """Async context manager mimicking httpx's ``client.stream(...)`` response."""

    def __init__(self, lines: list[str], status_code: int = 200, body: bytes = b"") -> None:
        self._lines = lines
        self.status_code = status_code
        self._body = body

    async def __aenter__(self) -> _FakeStreamResponse:
        return self

    async def __aexit__(self, *a: Any) -> None:
        return None

    async def aiter_lines(self):  # type: ignore[no-untyped-def]
        for line in self._lines:
            yield line

    async def aread(self) -> bytes:
        return self._body


class _FakeGatewayClient:
    """httpx.AsyncClient stand-in for the inline executor.

    * POST /tenant-keys/ephemeral → mint key (configurable)
    * GET  /tenant-keys           → tenant key fallback
    * GET  /tenant-defaults/resolve → default model
    * POST /tenant-keys/revoke-run → no-op (recorded)
    * stream POST /chat/completions → yields the configured SSE lines
    """

    def __init__(
        self,
        *,
        sse_lines: list[str],
        mint_key: str | None = "ek_inline",
        tenant_key: str | None = "tk_inline",
        default_model: str | None = "deepseek-chat",
        completion_status: int = 200,
        completion_error_body: bytes = b"",
    ) -> None:
        self.sse_lines = sse_lines
        self.mint_key = mint_key
        self.tenant_key = tenant_key
        self.default_model = default_model
        self.completion_status = completion_status
        self.completion_error_body = completion_error_body
        self.revoked: list[dict] = []
        self.chat_payloads: list[dict] = []

    async def __aenter__(self) -> _FakeGatewayClient:
        return self

    async def __aexit__(self, *a: Any) -> None:
        return None

    async def post(self, url: str, json: dict | None = None, **kw: Any):  # type: ignore[no-untyped-def]
        if url.endswith("/tenant-keys/ephemeral"):
            body = {"virtual_key": self.mint_key} if self.mint_key else {}
            return _JsonResp(200 if self.mint_key else 404, body)
        if url.endswith("/tenant-keys/revoke-run"):
            self.revoked.append(json or {})
            return _JsonResp(200, {})
        raise AssertionError(f"unexpected POST {url}")

    async def get(self, url: str, **kw: Any):  # type: ignore[no-untyped-def]
        if url.endswith("/tenant-keys"):
            if self.tenant_key:
                return _JsonResp(200, [{"virtual_key": self.tenant_key, "enabled": True}])
            return _JsonResp(200, [])
        if url.endswith("/tenant-defaults/resolve"):
            if self.default_model:
                return _JsonResp(200, {"model_id": self.default_model})
            return _JsonResp(200, {})
        raise AssertionError(f"unexpected GET {url}")

    def stream(self, method: str, url: str, json: dict | None = None, **kw: Any):  # type: ignore[no-untyped-def]
        assert method == "POST" and url.endswith("/chat/completions")
        self.chat_payloads.append(json or {})
        return _FakeStreamResponse(
            self.sse_lines,
            status_code=self.completion_status,
            body=self.completion_error_body,
        )


class _JsonResp:
    def __init__(self, status_code: int, payload: Any) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> Any:
        return self._payload


def _sse(obj: dict) -> str:
    return "data: " + json.dumps(obj)


def _usage_frame(prompt: int, completion: int, total: int) -> str:
    """A trailing usage SSE frame (empty choices, populated usage)."""
    return _sse(
        {
            "choices": [],
            "usage": {
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "total_tokens": total,
            },
        }
    )


# ─────────────────────────────────────────────────────────────────────────────
# _stream_completion_into_redis — wire mapping
# ─────────────────────────────────────────────────────────────────────────────


def test_stream_maps_delta_and_single_thinking_and_usage() -> None:
    """reasoning_content accumulates into ONE thinking entry; deltas map 1:1;
    usage is parsed from the trailing frame; the stream ends on [DONE]."""
    from agent_session_orchestrator.services import inline_run_executor as ex

    lines = [
        _sse({"choices": [{"delta": {"reasoning_content": "think"}}]}),
        _sse({"choices": [{"delta": {"reasoning_content": "ing..."}}]}),
        _sse({"choices": [{"delta": {"content": "Hello"}}]}),
        _sse({"choices": [{"delta": {"content": " world"}}]}),
        # trailing usage frame: empty choices, populated usage
        _usage_frame(12, 5, 17),
        "data: [DONE]",
    ]
    _FakeRedisStream.reset()
    redis_client = _FakeRedisStream.from_url()

    async def _run() -> tuple[dict, str]:
        client = _FakeGatewayClient(sse_lines=lines)
        return await ex._stream_completion_into_redis(
            client=client,  # type: ignore[arg-type]
            redis_client=redis_client,
            gateway_url="http://gw/api/llm-gateway/v1",
            tenant_id=TEST_TENANT,
            user_id=TEST_USER,
            session_id="ses_1",
            run_id="run_1",
            virtual_key="ek",
            model="deepseek-reasoner",
            messages=[{"role": "user", "content": "hi"}],
            sampling_params={},
        )

    usage, body, tool_calls, finish_reason = asyncio.get_event_loop().run_until_complete(_run())

    assert tool_calls == [] and finish_reason is None
    types_seen = [e[1]["type"] for e in redis_client.entries]
    # Exactly one thinking entry (never fragmented), two deltas, in order.
    assert types_seen == ["thinking", "delta", "delta"]
    thinking = next(e[1] for e in redis_client.entries if e[1]["type"] == "thinking")
    assert thinking["text"] == "thinking..."
    deltas = [e[1]["text"] for e in redis_client.entries if e[1]["type"] == "delta"]
    assert deltas == ["Hello", " world"]
    assert body == "Hello world"
    assert usage == {"input_tokens": 12, "output_tokens": 5, "total_tokens": 17}


def test_stream_raises_on_http_error_status() -> None:
    """A >=400 stream status raises InlineGatewayError (→ caller marks FAILED)."""
    from agent_session_orchestrator.services import inline_run_executor as ex

    _FakeRedisStream.reset()
    redis_client = _FakeRedisStream.from_url()

    async def _run() -> None:
        client = _FakeGatewayClient(
            sse_lines=[],
            completion_status=402,
            completion_error_body=b'{"error":{"code":"QUOTA_EXCEEDED"}}',
        )
        await ex._stream_completion_into_redis(
            client=client,  # type: ignore[arg-type]
            redis_client=redis_client,
            gateway_url="http://gw/api/llm-gateway/v1",
            tenant_id=TEST_TENANT, user_id=TEST_USER, session_id="ses_1",
            run_id="run_1", virtual_key="ek", model="m",
            messages=[{"role": "user", "content": "hi"}], sampling_params={},
        )

    raised = False
    try:
        asyncio.get_event_loop().run_until_complete(_run())
    except ex.InlineGatewayError as exc:
        raised = True
        assert "402" in str(exc)
    assert raised, "expected InlineGatewayError on 402"
    # No delta entries were written for an error response.
    assert not any(e[1]["type"] == "delta" for e in redis_client.entries)


def test_stream_raises_on_error_frame() -> None:
    """An inline ``{"error": {...}}`` SSE frame raises InlineGatewayError."""
    from agent_session_orchestrator.services import inline_run_executor as ex

    _FakeRedisStream.reset()
    redis_client = _FakeRedisStream.from_url()
    lines = [
        _sse({"choices": [{"delta": {"content": "partial"}}]}),
        _sse({"error": {"code": "PROVIDER.UPSTREAM_ERROR", "message": "boom"}}),
    ]

    async def _run() -> None:
        client = _FakeGatewayClient(sse_lines=lines)
        await ex._stream_completion_into_redis(
            client=client,  # type: ignore[arg-type]
            redis_client=redis_client,
            gateway_url="http://gw/api/llm-gateway/v1",
            tenant_id=TEST_TENANT, user_id=TEST_USER, session_id="ses_1",
            run_id="run_1", virtual_key="ek", model="m",
            messages=[{"role": "user", "content": "hi"}], sampling_params={},
        )

    raised = False
    try:
        asyncio.get_event_loop().run_until_complete(_run())
    except ex.InlineGatewayError as exc:
        raised = True
        assert "boom" in str(exc)
    assert raised


# ─────────────────────────────────────────────────────────────────────────────
# _execute_inline_run — full lifecycle (finalize persists via a real ORM run)
# ─────────────────────────────────────────────────────────────────────────────


def _install_fake_db(monkeypatch, run: Run) -> dict:
    """Patch acquire_db_session in the executor to yield a fake svc-backed db.

    The executor calls ``SessionService(db, ...).list_runs`` then mutates the
    returned Run.  We patch both ``acquire_db_session`` and ``SessionService``
    in the executor module so the finalize path operates on our real ``run``
    ORM instance and records message creations.
    """
    created_messages: list[dict] = []

    async def _fake_acquire(*, tenant_id: str, user_id: str = "SYSTEM", actor: str = "USER"):
        yield object()

    class _FakeSvc:
        def __init__(self, db: Any, *, tenant_id: str) -> None:
            pass

        async def list_runs(self, session_id: str) -> list[Run]:
            return [run]

        async def list_messages(self, session_id: str, *, cursor=None, limit=200):
            return [], None

        async def create_message(self, *, session_id, role, content, tool_calls=None, run_id=None):
            created_messages.append({"role": role, "content": content, "run_id": run_id})
            m = MagicMock()
            m.id = f"msg_{len(created_messages)}"
            return m

    from agent_session_orchestrator.services import inline_run_executor as ex

    monkeypatch.setattr(ex, "acquire_db_session", _fake_acquire)
    monkeypatch.setattr(ex, "SessionService", _FakeSvc)
    return {"messages": created_messages}


def test_execute_inline_run_completed_persists_and_tokens(monkeypatch) -> None:
    """Happy path: COMPLETED, tokens on the row, assistant + thinking persisted,
    key revoked, end marker present."""
    from agent_session_orchestrator.config import Settings
    from agent_session_orchestrator.services import inline_run_executor as ex

    run = Run(
        id="run_ok", tenant_id=TEST_TENANT, session_id="ses_1",
        task_description="hi", status="RUNNING", execution_mode="inline",
        sandbox_id=None, started_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
        created_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
    )
    captured = _install_fake_db(monkeypatch, run)

    settings = Settings(
        env="test", otel_enabled=False,
        redis_url="redis://x", llm_gateway_url="http://gw/api/llm-gateway/v1",
        knowledge_ingest_url="",  # skip memory write-back
        foundation_dlp_url="",
    )
    lines = [
        _sse({"choices": [{"delta": {"reasoning_content": "hmm"}}]}),
        _sse({"choices": [{"delta": {"content": "答案"}}]}),
        _usage_frame(3, 2, 5),
        "data: [DONE]",
    ]
    fake_client = _FakeGatewayClient(sse_lines=lines)
    _FakeRedisStream.reset()

    async def _run() -> None:
        with _patch_redis(_FakeRedisStream), \
                patch("httpx.AsyncClient", return_value=fake_client):
            await ex._execute_inline_run(
                settings=settings, tenant_id=TEST_TENANT, user_id=TEST_USER,
                session_id="ses_1", run_id="run_ok", task="hi",
                model_hint=None, sys_prompt=None, memory="", history=[],
                sampling_params={},
            )

    asyncio.get_event_loop().run_until_complete(_run())

    assert run.status == "COMPLETED"
    assert run.input_tokens == 3 and run.output_tokens == 2 and run.total_tokens == 5
    assert run.result_summary == "答案"
    # Assistant + thinking rows persisted via the shared replay helper.
    roles = [m["role"] for m in captured["messages"]]
    assert "assistant" in roles and "thinking" in roles
    assert any(m["role"] == "assistant" and m["content"] == "答案" for m in captured["messages"])
    # Ephemeral key revoked on completion.
    assert fake_client.revoked and fake_client.revoked[0]["run_id"] == "run_ok"
    # ``end`` marker pushed so the SSE tail terminates.
    redis_client = _FakeRedisStream.from_url()
    assert any(e[1]["type"] == "end" for e in redis_client.entries)
    # metadata carried run/session for gateway attribution.
    meta = fake_client.chat_payloads[0]["metadata"]
    assert meta["run_id"] == "run_ok" and meta["session_id"] == "ses_1"


def test_execute_inline_run_gateway_error_fails_without_assistant_message(monkeypatch) -> None:
    """A gateway error → FAILED + error_message, and NEVER an assistant message
    (historical bug: 402 quota body written as a reply).  End marker still pushed."""
    from agent_session_orchestrator.config import Settings
    from agent_session_orchestrator.services import inline_run_executor as ex

    run = Run(
        id="run_err", tenant_id=TEST_TENANT, session_id="ses_1",
        task_description="hi", status="RUNNING", execution_mode="inline",
        sandbox_id=None, started_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
        created_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
    )
    captured = _install_fake_db(monkeypatch, run)

    settings = Settings(
        env="test", otel_enabled=False, redis_url="redis://x",
        llm_gateway_url="http://gw/api/llm-gateway/v1",
        knowledge_ingest_url="", foundation_dlp_url="",
    )
    fake_client = _FakeGatewayClient(
        sse_lines=[],
        completion_status=402,
        completion_error_body=b'{"error":{"code":"QUOTA_EXCEEDED","message":"no quota"}}',
    )
    _FakeRedisStream.reset()

    async def _run() -> None:
        with _patch_redis(_FakeRedisStream), \
                patch("httpx.AsyncClient", return_value=fake_client):
            await ex._execute_inline_run(
                settings=settings, tenant_id=TEST_TENANT, user_id=TEST_USER,
                session_id="ses_1", run_id="run_err", task="hi",
                model_hint="m", sys_prompt=None, memory="", history=[],
                sampling_params={},
            )

    asyncio.get_event_loop().run_until_complete(_run())

    assert run.status == "FAILED"
    assert run.error_message and "402" in run.error_message
    # No assistant/thinking rows written on the error path.
    assert captured["messages"] == [], captured["messages"]
    # End marker still pushed so the frontend tail closes.
    redis_client = _FakeRedisStream.from_url()
    assert any(e[1]["type"] == "end" for e in redis_client.entries)


def test_execute_inline_run_no_key_fails(monkeypatch) -> None:
    """No mint + no tenant key → FAILED (no usable virtual key), no messages."""
    from agent_session_orchestrator.config import Settings
    from agent_session_orchestrator.services import inline_run_executor as ex

    run = Run(
        id="run_nokey", tenant_id=TEST_TENANT, session_id="ses_1",
        task_description="hi", status="RUNNING", execution_mode="inline",
        sandbox_id=None, started_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
        created_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
    )
    captured = _install_fake_db(monkeypatch, run)
    settings = Settings(
        env="test", otel_enabled=False, redis_url="redis://x",
        llm_gateway_url="http://gw/api/llm-gateway/v1",
        knowledge_ingest_url="", foundation_dlp_url="",
    )
    fake_client = _FakeGatewayClient(sse_lines=[], mint_key=None, tenant_key=None)
    _FakeRedisStream.reset()

    async def _run() -> None:
        with _patch_redis(_FakeRedisStream), \
                patch("httpx.AsyncClient", return_value=fake_client):
            await ex._execute_inline_run(
                settings=settings, tenant_id=TEST_TENANT, user_id=TEST_USER,
                session_id="ses_1", run_id="run_nokey", task="hi",
                model_hint="m", sys_prompt=None, memory="", history=[],
                sampling_params={},
            )

    asyncio.get_event_loop().run_until_complete(_run())
    assert run.status == "FAILED"
    assert "virtual key" in (run.error_message or "")
    assert captured["messages"] == []


def test_execute_inline_run_replay_is_idempotent(monkeypatch) -> None:
    """If assistant/tool/thinking rows already exist for the run, replay is a
    no-op (already_persisted_for_run guard) — no duplicate rows."""
    from agent_session_orchestrator.config import Settings
    from agent_session_orchestrator.services import inline_run_executor as ex

    run = Run(
        id="run_idem", tenant_id=TEST_TENANT, session_id="ses_1",
        task_description="hi", status="RUNNING", execution_mode="inline",
        sandbox_id=None, started_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
        created_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
    )

    created_messages: list[dict] = []

    async def _fake_acquire(*, tenant_id, user_id="SYSTEM", actor="USER"):
        yield object()

    existing = MagicMock()
    existing.run_id = "run_idem"
    existing.role = "assistant"

    class _FakeSvc:
        def __init__(self, db, *, tenant_id) -> None:
            pass

        async def list_runs(self, session_id):
            return [run]

        async def list_messages(self, session_id, *, cursor=None, limit=200):
            # An assistant row already exists for this run.
            return [existing], None

        async def create_message(self, **kw):
            created_messages.append(kw)
            m = MagicMock()
            m.id = "msg_x"
            return m

    monkeypatch.setattr(ex, "acquire_db_session", _fake_acquire)
    monkeypatch.setattr(ex, "SessionService", _FakeSvc)

    settings = Settings(
        env="test", otel_enabled=False, redis_url="redis://x",
        llm_gateway_url="http://gw/api/llm-gateway/v1",
        knowledge_ingest_url="", foundation_dlp_url="",
    )
    lines = [
        _sse({"choices": [{"delta": {"content": "dup"}}]}),
        _usage_frame(1, 1, 2),
        "data: [DONE]",
    ]
    fake_client = _FakeGatewayClient(sse_lines=lines)
    _FakeRedisStream.reset()

    async def _run() -> None:
        with _patch_redis(_FakeRedisStream), \
                patch("httpx.AsyncClient", return_value=fake_client):
            await ex._execute_inline_run(
                settings=settings, tenant_id=TEST_TENANT, user_id=TEST_USER,
                session_id="ses_1", run_id="run_idem", task="hi",
                model_hint="m", sys_prompt=None, memory="", history=[],
                sampling_params={},
            )

    asyncio.get_event_loop().run_until_complete(_run())
    assert run.status == "COMPLETED"
    # No new rows created — the existing assistant row satisfied the guard.
    assert created_messages == []


# ─────────────────────────────────────────────────────────────────────────────
# create_run endpoint — inline branch
# ─────────────────────────────────────────────────────────────────────────────


def test_create_run_inline_branch_no_scheduler(
    app_client: TestClient, mock_db: AsyncMock, auth_headers,
) -> None:
    """execution_mode='inline' flips the run to RUNNING, returns sandbox_id null,
    launches the background executor, and NEVER calls the scheduler."""
    session = Session(id="ses_inline", tenant_id=TEST_TENANT, user_id="u1", status="ACTIVE")
    mock_db.execute.return_value = make_scalar_result(session)

    async def _refresh(obj):
        if isinstance(obj, Run):
            obj.id = "run_inline"
            obj.created_at = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)

    mock_db.refresh.side_effect = _refresh

    scheduler_calls: list[str] = []
    quota_checks: list[dict] = []
    quota_records: list[dict] = []
    started: list[dict] = []

    class _FakeClient(AuthzAwareClient):
        async def get(self, url, **kw):
            # memory pre-fetch
            return __import__("httpx").Response(200, json={"hits": []})

        async def post(self, url, json=None, **kw):
            if "/check" in url and "authz" in url:
                return self._authz_allowed_resp()
            # Billing quota guard (dimension=agent_sessions) — allow.
            if url.endswith("/quota/check"):
                quota_checks.append(json or {})
                return __import__("httpx").Response(200, json={"allowed": True})
            if url.endswith("/quota/usage"):
                quota_records.append(json or {})
                return __import__("httpx").Response(200, json={})
            # Any scheduler /runs call would be a contract violation for inline.
            scheduler_calls.append(url)
            return __import__("httpx").Response(201, json={"sandbox_id": "x"})

    def _fake_start(**kw):
        started.append(kw)

    import httpx
    with patch.object(httpx, "AsyncClient", return_value=_FakeClient()), \
            patch(
                "agent_session_orchestrator.api.v1.runs.start_inline_run",
                new=_fake_start,
            ):
        resp = app_client.post(
            "/api/session/v1/sessions/ses_inline/runs",
            headers=auth_headers,
            json={"task": "你好", "execution_mode": "inline"},
        )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "RUNNING"
    assert body["sandbox_id"] is None
    assert body["execution_mode"] == "inline"
    # Scheduler was NOT called.
    assert scheduler_calls == [], scheduler_calls
    # Quota was checked (agent_sessions, 1) before the run and recorded after.
    assert len(quota_checks) == 1 and quota_checks[0]["dimension"] == "agent_sessions"
    assert len(quota_records) == 1 and quota_records[0]["dimension"] == "agent_sessions"
    # The background executor was launched with the right run id + task.
    assert started and started[0]["run_id"] == "run_inline"
    assert started[0]["task"] == "你好"


def test_create_run_inline_workspace_file_ids_reorder(
    app_client: TestClient, mock_db: AsyncMock, auth_headers, monkeypatch,
) -> None:
    """workspace_file_ids in the body hoists the referenced workspace files to the
    front of the list handed to start_inline_run; an unknown id is ignored."""
    from agent_session_orchestrator.services import workspace_context as wc

    session = Session(
        id="ses_wsref", tenant_id=TEST_TENANT, user_id="u1", status="ACTIVE",
        workspace_id="ws_1",
    )
    mock_db.execute.return_value = make_scalar_result(session)

    async def _refresh(obj):
        if isinstance(obj, Run):
            obj.id = "run_wsref"
            obj.created_at = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)

    mock_db.refresh.side_effect = _refresh

    # Fake workspace context: three files in workspace (created) order.
    class _WF:
        def __init__(self, id: str, filename: str) -> None:
            self.id = id
            self.blob_key = f"k/{id}"
            self.filename = filename
            self.mime = "text/plain"
            self.size_bytes = 10

    ctx = wc.WorkspaceContext(
        instructions=None,
        files=[_WF("wsf_1", "a.txt"), _WF("wsf_2", "b.txt"), _WF("wsf_3", "c.txt")],
        skills=[],
    )
    monkeypatch.setattr(
        wc, "workspace_context_injection_enabled", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(wc, "load_workspace_context", AsyncMock(return_value=ctx))

    started: list[dict] = []

    class _FakeClient(AuthzAwareClient):
        async def get(self, url, **kw):
            return __import__("httpx").Response(200, json={"hits": []})

        async def post(self, url, json=None, **kw):
            if "/check" in url and "authz" in url:
                return self._authz_allowed_resp()
            if url.endswith("/quota/check"):
                return __import__("httpx").Response(200, json={"allowed": True})
            if url.endswith("/quota/usage"):
                return __import__("httpx").Response(200, json={})
            raise AssertionError(f"unexpected POST {url}")

    def _fake_start(**kw):
        started.append(kw)

    import httpx
    with patch.object(httpx, "AsyncClient", return_value=_FakeClient()), \
            patch(
                "agent_session_orchestrator.api.v1.runs.start_inline_run",
                new=_fake_start,
            ):
        resp = app_client.post(
            "/api/session/v1/sessions/ses_wsref/runs",
            headers={**auth_headers, "X-User-Clearance": "L3"},
            json={
                "task": "hi",
                "execution_mode": "inline",
                "workspace_file_ids": ["wsf_3", "wsf_unknown"],
            },
        )

    assert resp.status_code == 201, resp.text
    assert started
    ws_files = started[0]["workspace_files"]
    # wsf_3 hoisted to the front; unknown id dropped; the rest keep workspace order.
    assert [f["id"] for f in ws_files] == ["wsf_3", "wsf_1", "wsf_2"]
    # Each dict carries the id so downstream can render the file-link convention.
    assert all("id" in f for f in ws_files)


def test_create_run_invalid_execution_mode_400(
    app_client: TestClient, mock_db: AsyncMock, auth_headers,
) -> None:
    """An unknown execution_mode is rejected before any run row is created."""
    session = Session(id="ses_x", tenant_id=TEST_TENANT, user_id="u1", status="ACTIVE")
    mock_db.execute.return_value = make_scalar_result(session)

    resp = app_client.post(
        "/api/session/v1/sessions/ses_x/runs",
        headers=auth_headers,
        json={"task": "hi", "execution_mode": "bogus"},
    )
    assert resp.status_code == 400
    assert "execution_mode" in resp.json()["detail"]


# ─────────────────────────────────────────────────────────────────────────────
# create_run endpoint — sandbox hard-guard for App / scheduled runs
# ─────────────────────────────────────────────────────────────────────────────


def _drive_guard_case(
    app_client: TestClient,
    mock_db: AsyncMock,
    auth_headers,
    *,
    body: dict,
) -> tuple[list[str], list[Run], list[dict], list[dict]]:
    """POST a create_run that requests inline but should be forced to sandbox.

    Returns (scheduler_urls, created_runs, started_inline_kwargs, quota_checks).
    Asserts a 201 and returns the interaction records for the caller to check.
    """
    session = Session(id="ses_g", tenant_id=TEST_TENANT, user_id="u1", status="ACTIVE")
    mock_db.execute.return_value = make_scalar_result(session)

    created_runs: list[Run] = []

    async def _refresh(obj):
        if isinstance(obj, Run):
            obj.id = "run_guard"
            obj.created_at = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
            created_runs.append(obj)

    mock_db.refresh.side_effect = _refresh

    scheduler_urls: list[str] = []
    quota_checks: list[dict] = []
    started: list[dict] = []

    class _FakeClient(AuthzAwareClient):
        async def get(self, url, **kw):
            return __import__("httpx").Response(200, json={"hits": []})

        async def post(self, url, json=None, **kw):
            if "/check" in url and "authz" in url:
                return self._authz_allowed_resp()
            if url.endswith("/quota/check"):
                quota_checks.append(json or {})
                return __import__("httpx").Response(200, json={"allowed": True})
            if url.endswith("/quota/usage"):
                return __import__("httpx").Response(200, json={})
            # A scheduler /runs POST — the sandbox path.
            scheduler_urls.append(url)
            return __import__("httpx").Response(201, json={"sandbox_id": "sbx012345678"})

    def _fake_start(**kw):
        started.append(kw)

    import httpx
    with patch.object(httpx, "AsyncClient", return_value=_FakeClient()), \
            patch(
                "agent_session_orchestrator.api.v1.runs.start_inline_run",
                new=_fake_start,
            ):
        resp = app_client.post(
            "/api/session/v1/sessions/ses_g/runs",
            headers=auth_headers,
            json=body,
        )

    assert resp.status_code == 201, resp.text
    return scheduler_urls, created_runs, started, quota_checks


def test_scheduled_inline_run_forced_to_sandbox(
    app_client: TestClient, mock_db: AsyncMock, auth_headers,
) -> None:
    """source='schedule' + execution_mode='inline' → forced to sandbox: the run
    row is persisted with execution_mode='sandbox', the scheduler IS called, and
    the inline executor is NOT launched (nor is inline quota checked)."""
    scheduler_urls, created_runs, started, quota_checks = _drive_guard_case(
        app_client, mock_db, auth_headers,
        body={"task": "cron work", "execution_mode": "inline", "source": "schedule"},
    )
    assert created_runs and created_runs[0].execution_mode == "sandbox"
    assert created_runs[0].source == "schedule"
    # Sandbox path taken: scheduler called, inline executor NOT launched.
    assert scheduler_urls and scheduler_urls[0].endswith("/runs")
    assert started == [], "inline executor must not run for a scheduled run"
    # Inline quota check is skipped (sandbox path is metered by the scheduler).
    assert quota_checks == [], "inline quota must not be checked on the sandbox path"


def test_app_snapshot_inline_run_forced_to_sandbox(
    app_client: TestClient, mock_db: AsyncMock, auth_headers,
) -> None:
    """A run carrying app_snapshot + execution_mode='inline' → forced to sandbox
    even though source='user'."""
    scheduler_urls, created_runs, started, quota_checks = _drive_guard_case(
        app_client, mock_db, auth_headers,
        body={
            "task": "run app",
            "execution_mode": "inline",
            "app_snapshot": {"app_id": "app_1", "version": 3},
        },
    )
    assert created_runs and created_runs[0].execution_mode == "sandbox"
    assert created_runs[0].app_snapshot == {"app_id": "app_1", "version": 3}
    assert scheduler_urls and scheduler_urls[0].endswith("/runs")
    assert started == [], "inline executor must not run for an App run"
    assert quota_checks == []


def test_webhook_source_accepted_and_forced_to_sandbox(
    app_client: TestClient, mock_db: AsyncMock, auth_headers,
) -> None:
    """source='webhook' is a valid run origin; like 'schedule' it is a
    non-interactive run, so an inline request is forced back to sandbox and the
    scheduler is called.  A 201 (asserted inside the helper) proves the source
    passed the create_run allowlist; the persisted row records source='webhook'.
    """
    scheduler_urls, created_runs, started, quota_checks = _drive_guard_case(
        app_client, mock_db, auth_headers,
        body={"task": "hook work", "execution_mode": "inline", "source": "webhook"},
    )
    assert created_runs and created_runs[0].execution_mode == "sandbox"
    assert created_runs[0].source == "webhook"
    assert scheduler_urls and scheduler_urls[0].endswith("/runs")
    assert started == [], "inline executor must not run for a webhook run"
    assert quota_checks == []


def test_create_run_invalid_source_400(
    app_client: TestClient, mock_db: AsyncMock, auth_headers,
) -> None:
    """An unknown source is rejected before any run row is created."""
    session = Session(id="ses_s", tenant_id=TEST_TENANT, user_id="u1", status="ACTIVE")
    mock_db.execute.return_value = make_scalar_result(session)

    resp = app_client.post(
        "/api/session/v1/sessions/ses_s/runs",
        headers=auth_headers,
        json={"task": "hi", "source": "bot"},
    )
    assert resp.status_code == 400
    assert "source" in resp.json()["detail"]


# ─────────────────────────────────────────────────────────────────────────────
# create_run endpoint — inline quota (fail-closed)
# ─────────────────────────────────────────────────────────────────────────────


def test_inline_quota_exceeded_returns_402_no_running_run(
    app_client: TestClient, mock_db: AsyncMock, auth_headers,
) -> None:
    """A rejected quota check (allowed=false) → 402 and NO run row is created
    (the check runs before create_run, so no RUNNING orphan survives)."""
    session = Session(id="ses_q", tenant_id=TEST_TENANT, user_id="u1", status="ACTIVE")
    mock_db.execute.return_value = make_scalar_result(session)

    started: list[dict] = []

    class _FakeClient(AuthzAwareClient):
        async def get(self, url, **kw):
            return __import__("httpx").Response(200, json={"hits": []})

        async def post(self, url, json=None, **kw):
            if "/check" in url and "authz" in url:
                return self._authz_allowed_resp()
            if url.endswith("/quota/check"):
                # Quota exhausted.
                return __import__("httpx").Response(
                    200,
                    json={
                        "allowed": False, "dimension": "agent_sessions",
                        "current": 10, "limit": 10, "remaining": 0,
                    },
                )
            raise AssertionError(f"unexpected POST {url}")

    def _fake_start(**kw):
        started.append(kw)

    import httpx
    with patch.object(httpx, "AsyncClient", return_value=_FakeClient()), \
            patch(
                "agent_session_orchestrator.api.v1.runs.start_inline_run",
                new=_fake_start,
            ):
        resp = app_client.post(
            "/api/session/v1/sessions/ses_q/runs",
            headers=auth_headers,
            json={"task": "hi", "execution_mode": "inline"},
        )

    assert resp.status_code == 402, resp.text
    detail = resp.json()["detail"]
    assert detail["message"] == "quota exceeded"
    assert detail["dimension"] == "agent_sessions"
    # No run row was ever created → no RUNNING orphan (db.add never saw a Run).
    added_runs = [
        c.args[0] for c in mock_db.add.call_args_list
        if c.args and isinstance(c.args[0], Run)
    ]
    assert added_runs == [], "no Run must be created when quota is exceeded"
    # The inline executor was never launched.
    assert started == []


def test_inline_quota_billing_unreachable_returns_503(
    app_client: TestClient, mock_db: AsyncMock, auth_headers,
) -> None:
    """Billing unreachable during the inline quota check → 503 (fail-closed), and
    no run row / executor launch."""
    session = Session(id="ses_q2", tenant_id=TEST_TENANT, user_id="u1", status="ACTIVE")
    mock_db.execute.return_value = make_scalar_result(session)

    started: list[dict] = []

    class _FakeClient(AuthzAwareClient):
        async def get(self, url, **kw):
            return __import__("httpx").Response(200, json={"hits": []})

        async def post(self, url, json=None, **kw):
            if "/check" in url and "authz" in url:
                return self._authz_allowed_resp()
            if url.endswith("/quota/check"):
                raise __import__("httpx").ConnectError("billing down")
            raise AssertionError(f"unexpected POST {url}")

    def _fake_start(**kw):
        started.append(kw)

    import httpx
    with patch.object(httpx, "AsyncClient", return_value=_FakeClient()), \
            patch(
                "agent_session_orchestrator.api.v1.runs.start_inline_run",
                new=_fake_start,
            ):
        resp = app_client.post(
            "/api/session/v1/sessions/ses_q2/runs",
            headers=auth_headers,
            json={"task": "hi", "execution_mode": "inline"},
        )

    assert resp.status_code == 503, resp.text
    added_runs = [
        c.args[0] for c in mock_db.add.call_args_list
        if c.args and isinstance(c.args[0], Run)
    ]
    assert added_runs == []
    assert started == []


# ─────────────────────────────────────────────────────────────────────────────
# reasoning_effort passthrough (accept-and-ignore on the current gateway)
# ─────────────────────────────────────────────────────────────────────────────


def test_create_run_inline_threads_reasoning_effort(
    app_client: TestClient, mock_db: AsyncMock, auth_headers,
) -> None:
    """reasoning_effort on the create_run body is passed to start_inline_run."""
    session = Session(id="ses_re", tenant_id=TEST_TENANT, user_id="u1", status="ACTIVE")
    mock_db.execute.return_value = make_scalar_result(session)

    async def _refresh(obj):
        if isinstance(obj, Run):
            obj.id = "run_re"
            obj.created_at = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)

    mock_db.refresh.side_effect = _refresh

    started: list[dict] = []

    class _FakeClient(AuthzAwareClient):
        async def get(self, url, **kw):
            return __import__("httpx").Response(200, json={"hits": []})

        async def post(self, url, json=None, **kw):
            if "/check" in url and "authz" in url:
                return self._authz_allowed_resp()
            if url.endswith("/quota/check"):
                return __import__("httpx").Response(200, json={"allowed": True})
            if url.endswith("/quota/usage"):
                return __import__("httpx").Response(200, json={})
            raise AssertionError(f"unexpected POST {url}")

    def _fake_start(**kw):
        started.append(kw)

    import httpx
    with patch.object(httpx, "AsyncClient", return_value=_FakeClient()), \
            patch(
                "agent_session_orchestrator.api.v1.runs.start_inline_run",
                new=_fake_start,
            ):
        resp = app_client.post(
            "/api/session/v1/sessions/ses_re/runs",
            headers=auth_headers,
            json={"task": "hi", "execution_mode": "inline", "reasoning_effort": "high"},
        )

    assert resp.status_code == 201, resp.text
    assert started and started[0]["reasoning_effort"] == "high"


# ─────────────────────────────────────────────────────────────────────────────
# mcp_server_ids presence semantics (inline branch)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("include_key", "request_ids", "expected_arg"),
    [
        (False, None, None),
        (True, [], []),
        (True, ["mcp_a", "mcp_b"], ["mcp_a", "mcp_b"]),
    ],
    ids=["omitted-none", "explicit-empty-disables-all", "explicit-list-narrows"],
)
def test_create_run_inline_threads_mcp_server_ids(
    app_client: TestClient,
    mock_db: AsyncMock,
    auth_headers,
    include_key: bool,
    request_ids: list[str] | None,
    expected_arg: list[str] | None,
) -> None:
    """Inline branch hands the presence-resolved MCP selection to the executor.

    ``None`` (key omitted → tenant default set) and ``[]`` (explicitly disable
    all MCP) must arrive at ``start_inline_run`` as DISTINCT values — the
    inline catalog loader keys off exactly that difference.
    """
    session = Session(
        id="ses_mcp", tenant_id=TEST_TENANT, user_id="u1", status="ACTIVE",
    )
    mock_db.execute.return_value = make_scalar_result(session)

    async def _refresh(obj):
        if isinstance(obj, Run):
            obj.id = "run_mcp"
            obj.created_at = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)

    mock_db.refresh.side_effect = _refresh

    started: list[dict] = []

    class _FakeClient(AuthzAwareClient):
        async def get(self, url, **kw):
            return __import__("httpx").Response(200, json={"hits": []})

        async def post(self, url, json=None, **kw):
            if "/check" in url and "authz" in url:
                return self._authz_allowed_resp()
            if url.endswith("/quota/check"):
                return __import__("httpx").Response(200, json={"allowed": True})
            if url.endswith("/quota/usage"):
                return __import__("httpx").Response(200, json={})
            raise AssertionError(f"unexpected POST {url}")

    def _fake_start(**kw):
        started.append(kw)

    body: dict = {"task": "hi", "execution_mode": "inline"}
    if include_key:
        body["mcp_server_ids"] = request_ids

    import httpx
    with patch.object(httpx, "AsyncClient", return_value=_FakeClient()), \
            patch(
                "agent_session_orchestrator.api.v1.runs.start_inline_run",
                new=_fake_start,
            ):
        resp = app_client.post(
            "/api/session/v1/sessions/ses_mcp/runs",
            headers=auth_headers,
            json=body,
        )

    assert resp.status_code == 201, resp.text
    assert started, "inline executor was not started"
    assert started[0]["mcp_server_ids"] == expected_arg


def test_stream_payload_includes_reasoning_effort_when_set() -> None:
    """When reasoning_effort is provided, it lands on the /chat/completions
    payload verbatim; when None it is omitted (no clamp, original passthrough)."""
    from agent_session_orchestrator.services import inline_run_executor as ex

    lines = [
        _sse({"choices": [{"delta": {"content": "ok"}}]}),
        _usage_frame(1, 1, 2),
        "data: [DONE]",
    ]

    async def _run(reasoning_effort):
        _FakeRedisStream.reset()
        redis_client = _FakeRedisStream.from_url()
        client = _FakeGatewayClient(sse_lines=lines)
        await ex._stream_completion_into_redis(
            client=client,  # type: ignore[arg-type]
            redis_client=redis_client,
            gateway_url="http://gw/api/llm-gateway/v1",
            tenant_id=TEST_TENANT, user_id=TEST_USER, session_id="ses_1",
            run_id="run_1", virtual_key="ek", model="deepseek-reasoner",
            messages=[{"role": "user", "content": "hi"}], sampling_params={},
            reasoning_effort=reasoning_effort,
        )
        return client.chat_payloads[0]

    payload_high = asyncio.get_event_loop().run_until_complete(_run("high"))
    assert payload_high["reasoning_effort"] == "high"

    payload_none = asyncio.get_event_loop().run_until_complete(_run(None))
    assert "reasoning_effort" not in payload_none


# ─────────────────────────────────────────────────────────────────────────────
# cancel — inline branch
# ─────────────────────────────────────────────────────────────────────────────


def test_cancel_inline_run_does_not_call_scheduler(
    app_client: TestClient, mock_db: AsyncMock, auth_headers,
) -> None:
    """Cancelling an inline run tears down in-process state (task/end/revoke) and
    never hits the scheduler; the row is flipped to CANCELLED."""
    run = Run(
        id="run_c", tenant_id=TEST_TENANT, session_id="ses_1",
        task_description="t", status="RUNNING", execution_mode="inline",
        sandbox_id=None, started_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
        created_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
    )
    ses = Session(id="ses_1", tenant_id=TEST_TENANT, user_id="u1", status="ACTIVE")
    seq_result = MagicMock()
    seq_result.scalar_one_or_none = MagicMock(return_value=0)
    mock_db.execute.side_effect = [
        make_scalar_result(run),   # get_run
        make_scalar_result(ses),   # create_message → get_session
        seq_result,                # create_message → sequence probe
    ]

    async def _refresh(obj):
        obj.id = "msg_c"
        obj.created_at = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)

    mock_db.refresh.side_effect = _refresh

    scheduler_calls: list[str] = []
    cancel_calls: list[dict] = []

    class _FakeClient(AuthzAwareClient):
        async def post(self, url, **kw):
            if "/check" in url and "authz" in url:
                return self._authz_allowed_resp()
            scheduler_calls.append(url)
            return __import__("httpx").Response(200, json={})

    async def _fake_cancel(**kw):
        cancel_calls.append(kw)

    import httpx
    with patch.object(httpx, "AsyncClient", return_value=_FakeClient()), \
            patch(
                "agent_session_orchestrator.api.v1.runs.cancel_inline_run",
                new=_fake_cancel,
            ):
        resp = app_client.post(
            "/api/session/v1/sessions/ses_1/runs/run_c/cancel",
            headers=auth_headers,
        )

    assert resp.status_code == 200
    assert resp.json()["status"] == "CANCELLED"
    # Inline cancel path was taken; scheduler cancel endpoint NOT called.
    assert cancel_calls and cancel_calls[0]["run_id"] == "run_c"
    assert scheduler_calls == [], scheduler_calls


def test_cancel_inline_run_helper_pushes_end_and_revokes() -> None:
    """cancel_inline_run pushes an ``end`` marker and best-effort revokes the key
    even when no task handle is registered (already finished / process restart)."""
    from agent_session_orchestrator.config import Settings
    from agent_session_orchestrator.services import inline_run_executor as ex

    settings = Settings(
        env="test", otel_enabled=False, redis_url="redis://x",
        llm_gateway_url="http://gw/api/llm-gateway/v1",
    )
    fake_client = _FakeGatewayClient(sse_lines=[])
    _FakeRedisStream.reset()

    async def _run() -> None:
        with _patch_redis(_FakeRedisStream), \
                patch("httpx.AsyncClient", return_value=fake_client):
            await ex.cancel_inline_run(
                settings=settings, tenant_id=TEST_TENANT, user_id=TEST_USER,
                run_id="run_gone",
            )

    asyncio.get_event_loop().run_until_complete(_run())
    redis_client = _FakeRedisStream.from_url()
    assert any(e[1]["type"] == "end" for e in redis_client.entries)
    assert fake_client.revoked and fake_client.revoked[0]["run_id"] == "run_gone"


# ─────────────────────────────────────────────────────────────────────────────
# sweeper — inline orphan branch
# ─────────────────────────────────────────────────────────────────────────────


def test_sweeper_fails_orphaned_inline_run() -> None:
    """An inline run stale past INLINE_ORPHAN_AFTER_SECONDS is force-FAILED with
    the orphan message + an ``end`` marker; a fresh inline run is left alone."""
    from agent_session_orchestrator.services import reconcile_sweeper as sw

    stale_age = dt.timedelta(seconds=sw.INLINE_ORPHAN_AFTER_SECONDS + 60)
    old = Run(
        id="run_old", tenant_id=TEST_TENANT, session_id="ses_1",
        task_description="t", status="RUNNING", execution_mode="inline",
        sandbox_id=None,
        started_at=dt.datetime.now(tz=dt.UTC) - stale_age,
        created_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
    )
    fresh = Run(
        id="run_fresh", tenant_id=TEST_TENANT, session_id="ses_1",
        task_description="t", status="RUNNING", execution_mode="inline",
        sandbox_id=None,
        started_at=dt.datetime.now(tz=dt.UTC),
        created_at=dt.datetime.now(tz=dt.UTC),
    )
    assert sw._inline_run_is_orphaned(old) is True
    assert sw._inline_run_is_orphaned(fresh) is False

    _FakeRedisStream.reset()

    async def _run() -> None:
        with _patch_redis(_FakeRedisStream):
            await sw._fail_orphaned_inline_run(run_obj=old, redis_url="redis://x")

    asyncio.get_event_loop().run_until_complete(_run())
    assert old.status == "FAILED"
    assert "orphaned" in (old.error_message or "")
    redis_client = _FakeRedisStream.from_url()
    assert any(e[1]["type"] == "end" for e in redis_client.entries)


# ─────────────────────────────────────────────────────────────────────────────
# canvas (canvas v2, Phase B) — model-triggered canvas on the inline path
# ─────────────────────────────────────────────────────────────────────────────


def _tool_call_frames(
    *, call_id: str, name: str, arguments: str, finish: str = "tool_calls"
) -> list[str]:
    """SSE frames streaming one OpenAI tool call across chunks, then a finish.

    Mirrors how DeepSeek/OpenAI stream tool calls: the first fragment carries
    ``id`` + ``function.name``, subsequent fragments append ``function.arguments``,
    and a final choice carries ``finish_reason``.
    """
    mid = len(arguments) // 2
    return [
        _sse(
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": call_id,
                                    "type": "function",
                                    "function": {"name": name, "arguments": arguments[:mid]},
                                }
                            ]
                        },
                    }
                ]
            }
        ),
        _sse(
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {"index": 0, "function": {"arguments": arguments[mid:]}}
                            ]
                        },
                    }
                ]
            }
        ),
        _sse({"choices": [{"index": 0, "delta": {}, "finish_reason": finish}]}),
    ]


# A canvas body comfortably above the guard (>=200 chars, >=2 blank-line blocks).
_CANVAS_BODY = (
    "# 人工智能与就业\n\n"
    + ("人工智能正在深刻改变各行各业的工作形态与技能需求。" * 6)
    + "\n\n"
    + ("劳动者应主动学习新工具，把重复性工作交给自动化。" * 6)
)


class _MultiRoundGatewayClient(_FakeGatewayClient):
    """Gateway client that returns a different SSE script per /chat/completions call.

    ``sse_scripts`` is a list of line-lists, one per completion round; the Nth
    round POST returns the Nth script.  Used to drive the tool loop (round 1
    tool_call → round 2 pointer reply).
    """

    def __init__(self, *, sse_scripts: list[list[str]], **kw: Any) -> None:
        super().__init__(sse_lines=sse_scripts[0] if sse_scripts else [], **kw)
        self._scripts = sse_scripts
        self._round = 0

    def stream(self, method: str, url: str, json: dict | None = None, **kw: Any):  # type: ignore[no-untyped-def]
        assert method == "POST" and url.endswith("/chat/completions")
        self.chat_payloads.append(json or {})
        script = (
            self._scripts[self._round]
            if self._round < len(self._scripts)
            else ["data: [DONE]"]
        )
        self._round += 1
        return _FakeStreamResponse(script, status_code=self.completion_status)


def _canvas_db_and_run(run_id: str = "run_cvs"):
    """A FakeDB seeded with a session + the inline run, and wiring so the executor
    materializes the canvas via the real CanvasService and finalizes via a real
    SessionService — both against the same in-memory FakeDB.

    Returns ``(db, run, install)`` where ``install(monkeypatch)`` patches
    ``acquire_db_session`` (yields the shared db) and ``SessionService.list_runs``
    (FakeDB does not model Run queries) on the executor module.
    """
    from tests.test_canvas_service import FakeDB, _seed_session

    db = FakeDB()
    _seed_session(db)
    run = Run(
        id=run_id, tenant_id=TEST_TENANT, session_id="ses_1",
        task_description="写一篇《AI 与就业》短文", status="RUNNING",
        execution_mode="inline", sandbox_id=None,
        started_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
        created_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
    )

    def install(monkeypatch) -> None:
        from agent_session_orchestrator.services import inline_run_executor as ex
        from agent_session_orchestrator.services.session_service import SessionService

        async def _fake_acquire(*, tenant_id, user_id="SYSTEM", actor="USER"):
            yield db

        async def _fake_list_runs(self, session_id):
            return [run]

        monkeypatch.setattr(ex, "acquire_db_session", _fake_acquire)
        monkeypatch.setattr(SessionService, "list_runs", _fake_list_runs)
        # Canvas flag ON without a live feature-flag service.
        monkeypatch.setattr(ex, "_canvas_enabled_for_tenant", AsyncMock(return_value=True))

    return db, run, install


def test_inline_run_advertises_tools_and_materializes_canvas(monkeypatch) -> None:
    """Round 1: the request advertises tools=[create_canvas] and the model calls
    it → the canvas is created in-DB, a ``canvas`` token-stream entry is written
    (relayed as canvas_created), the tool loop closes with a pointer reply, and the
    assistant message is backfilled with canvas_id at finalize."""
    from agent_session_orchestrator.config import Settings
    from agent_session_orchestrator.services import inline_run_executor as ex

    db, run, install = _canvas_db_and_run()
    install(monkeypatch)

    settings = Settings(
        env="test", otel_enabled=False, redis_url="redis://x",
        llm_gateway_url="http://gw/api/llm-gateway/v1",
        knowledge_ingest_url="", foundation_dlp_url="",
    )
    args = json.dumps({"title": "AI 与就业", "content_markdown": _CANVAS_BODY, "kind": "article"})
    scripts = [
        # Round 1: the model calls create_canvas.
        _tool_call_frames(call_id="call_1", name="create_canvas", arguments=args)
        + [_usage_frame(20, 8, 28)],
        # Round 2: after the tool result, the model writes its pointer reply.
        [
            _sse({"choices": [{"index": 0, "delta": {"content": "已为你创作《AI 与就业》。"}}]}),
            _sse({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}),
            _usage_frame(30, 6, 36),
            "data: [DONE]",
        ],
    ]
    fake_client = _MultiRoundGatewayClient(sse_scripts=scripts)
    _FakeRedisStream.reset()

    async def _run() -> None:
        with _patch_redis(_FakeRedisStream), \
                patch("httpx.AsyncClient", return_value=fake_client):
            await ex._execute_inline_run(
                settings=settings, tenant_id=TEST_TENANT, user_id=TEST_USER,
                session_id="ses_1", run_id="run_cvs", task="写一篇《AI 与就业》短文",
                model_hint="deepseek-chat", sys_prompt=None, memory="", history=[],
                sampling_params={},
            )

    asyncio.get_event_loop().run_until_complete(_run())

    assert run.status == "COMPLETED"
    # Round-1 request advertised the create_canvas tool.
    round1 = fake_client.chat_payloads[0]
    tool_names = [t["function"]["name"] for t in round1.get("tools", [])]
    assert "create_canvas" in tool_names
    # A written-creation prompt forced the tool on round 1.
    assert round1.get("tool_choice", {}).get("function", {}).get("name") == "create_canvas"
    # Exactly one canvas materialized in-DB, from this run, with the model's title.
    assert len(db.canvases) == 1
    assert db.canvases[0].source_run_id == "run_cvs"
    assert db.canvases[0].title == "AI 与就业"
    # A ``canvas`` token-stream entry was written (→ canvas_created SSE relay).
    redis_client = _FakeRedisStream.from_url()
    canvas_entries = [e[1] for e in redis_client.entries if e[1]["type"] == "canvas"]
    assert len(canvas_entries) == 1
    assert canvas_entries[0]["canvas_id"] == db.canvases[0].id
    # The pointer reply from round 2 streamed as delta tokens.
    deltas = "".join(e[1]["text"] for e in redis_client.entries if e[1]["type"] == "delta")
    assert "已为你创作" in deltas
    # Second round fed the tool result back (assistant tool_calls + tool role).
    round2_msgs = fake_client.chat_payloads[1]["messages"]
    assert any(m.get("role") == "tool" for m in round2_msgs)
    # The assistant message persisted at finalize carries the canvas_id.
    assistant_msgs = [m for m in db.messages if m.role == "assistant" and m.run_id == "run_cvs"]
    assert assistant_msgs and assistant_msgs[-1].canvas_id == db.canvases[0].id


def test_inline_run_single_canvas_per_run_guard(monkeypatch) -> None:
    """Two create_canvas tool calls across rounds materialize only ONE canvas
    (the single-canvas-per-run cap no-ops the repeat)."""
    from agent_session_orchestrator.config import Settings
    from agent_session_orchestrator.services import inline_run_executor as ex

    db, run, install = _canvas_db_and_run(run_id="run_dup")
    install(monkeypatch)

    settings = Settings(
        env="test", otel_enabled=False, redis_url="redis://x",
        llm_gateway_url="http://gw/api/llm-gateway/v1",
        knowledge_ingest_url="", foundation_dlp_url="",
    )
    args1 = json.dumps({"title": "First", "content_markdown": _CANVAS_BODY})
    args2 = json.dumps({"title": "Second", "content_markdown": _CANVAS_BODY})
    scripts = [
        _tool_call_frames(call_id="c1", name="create_canvas", arguments=args1)
        + [_usage_frame(10, 4, 14)],
        # Round 2 also calls create_canvas — the cap must make it a no-op.
        _tool_call_frames(call_id="c2", name="create_canvas", arguments=args2)
        + [_usage_frame(10, 4, 14)],
    ]
    fake_client = _MultiRoundGatewayClient(sse_scripts=scripts)
    _FakeRedisStream.reset()

    async def _run() -> None:
        with _patch_redis(_FakeRedisStream), \
                patch("httpx.AsyncClient", return_value=fake_client):
            await ex._execute_inline_run(
                settings=settings, tenant_id=TEST_TENANT, user_id=TEST_USER,
                session_id="ses_1", run_id="run_dup", task="写一篇文章",
                model_hint="deepseek-chat", sys_prompt=None, memory="", history=[],
                sampling_params={},
            )

    asyncio.get_event_loop().run_until_complete(_run())

    # Exactly one canvas despite two create_canvas tool calls; it is the first.
    assert len(db.canvases) == 1
    assert db.canvases[0].title == "First"
    # Only one ``canvas`` token-stream entry emitted.
    redis_client = _FakeRedisStream.from_url()
    assert len([e for e in redis_client.entries if e[1]["type"] == "canvas"]) == 1
    # The loop closed as soon as the model stopped calling a known tool: two
    # create_canvas rounds (from the 2 scripts) + one terminating round (the
    # client returns [DONE] past its scripts, i.e. no tool call → break).  It
    # never approaches the cap.
    assert len(fake_client.chat_payloads) == 3
    assert len(fake_client.chat_payloads) < ex._INLINE_MAX_TOOL_ROUNDS


def test_inline_run_canvas_disabled_sends_no_tools(monkeypatch) -> None:
    """When the canvas flag is OFF the request advertises no tools and no canvas
    is created even if the prompt looks like written creation."""
    from agent_session_orchestrator.config import Settings
    from agent_session_orchestrator.services import inline_run_executor as ex

    run = Run(
        id="run_off", tenant_id=TEST_TENANT, session_id="ses_1",
        task_description="写一篇文章", status="RUNNING", execution_mode="inline",
        sandbox_id=None, started_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
        created_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
    )
    _install_fake_db(monkeypatch, run)
    monkeypatch.setattr(ex, "_canvas_enabled_for_tenant", AsyncMock(return_value=False))

    settings = Settings(
        env="test", otel_enabled=False, redis_url="redis://x",
        llm_gateway_url="http://gw/api/llm-gateway/v1",
        knowledge_ingest_url="", foundation_dlp_url="",
    )
    lines = [
        _sse({"choices": [{"index": 0, "delta": {"content": "普通回复"}}]}),
        _usage_frame(3, 2, 5),
        "data: [DONE]",
    ]
    fake_client = _FakeGatewayClient(sse_lines=lines)
    _FakeRedisStream.reset()

    async def _run() -> None:
        with _patch_redis(_FakeRedisStream), \
                patch("httpx.AsyncClient", return_value=fake_client):
            await ex._execute_inline_run(
                settings=settings, tenant_id=TEST_TENANT, user_id=TEST_USER,
                session_id="ses_1", run_id="run_off", task="写一篇文章",
                model_hint="deepseek-chat", sys_prompt=None, memory="", history=[],
                sampling_params={},
            )

    asyncio.get_event_loop().run_until_complete(_run())

    assert run.status == "COMPLETED"
    # No tools advertised, no tool_choice — a plain completion.
    assert "tools" not in fake_client.chat_payloads[0]
    assert "tool_choice" not in fake_client.chat_payloads[0]
    # No canvas token-stream entry.
    redis_client = _FakeRedisStream.from_url()
    assert not any(e[1]["type"] == "canvas" for e in redis_client.entries)


# ── written-creation heuristic (tool_choice forcing) ─────────────────────────


def test_is_written_creation_prompt_hits_and_misses() -> None:
    from agent_session_orchestrator.services.canvas_tool_spec import (
        is_written_creation_prompt,
    )

    # Hits: write-verb + genre, or a 《书名号》 titled assignment.
    assert is_written_creation_prompt("写一篇关于AI的短文") is True
    assert is_written_creation_prompt("帮我创作一封道歉邮件") is True
    assert is_written_creation_prompt("写一篇《春天的故事》") is True
    assert is_written_creation_prompt("draft a report on Q3 results") is True
    # Misses: no write-verb, or a verb without a written-work genre.
    assert is_written_creation_prompt("今天天气怎么样？") is False
    assert is_written_creation_prompt("帮我算一下 3+5") is False
    assert is_written_creation_prompt("写个函数把列表去重") is False
    assert is_written_creation_prompt("") is False


def test_inline_run_forces_tool_choice_only_for_written_creation(monkeypatch) -> None:
    """A non-written-creation prompt advertises the tool but does NOT force it
    (tool_choice='auto'); a written-creation prompt forces create_canvas."""
    from agent_session_orchestrator.config import Settings
    from agent_session_orchestrator.services import inline_run_executor as ex

    run = Run(
        id="run_auto", tenant_id=TEST_TENANT, session_id="ses_1",
        task_description="今天几号", status="RUNNING", execution_mode="inline",
        sandbox_id=None, started_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
        created_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
    )
    _install_fake_db(monkeypatch, run)
    monkeypatch.setattr(ex, "_canvas_enabled_for_tenant", AsyncMock(return_value=True))

    settings = Settings(
        env="test", otel_enabled=False, redis_url="redis://x",
        llm_gateway_url="http://gw/api/llm-gateway/v1",
        knowledge_ingest_url="", foundation_dlp_url="",
    )
    lines = [
        _sse({"choices": [{"index": 0, "delta": {"content": "今天是7月14日"}}]}),
        _usage_frame(3, 2, 5),
        "data: [DONE]",
    ]
    fake_client = _FakeGatewayClient(sse_lines=lines)
    _FakeRedisStream.reset()

    async def _run() -> None:
        with _patch_redis(_FakeRedisStream), \
                patch("httpx.AsyncClient", return_value=fake_client):
            await ex._execute_inline_run(
                settings=settings, tenant_id=TEST_TENANT, user_id=TEST_USER,
                session_id="ses_1", run_id="run_auto", task="今天几号？",
                model_hint="deepseek-chat", sys_prompt=None, memory="", history=[],
                sampling_params={},
            )

    asyncio.get_event_loop().run_until_complete(_run())

    payload = fake_client.chat_payloads[0]
    # Tool is advertised, but not forced for a non-written-creation prompt.
    assert any(t["function"]["name"] == "create_canvas" for t in payload["tools"])
    assert payload.get("tool_choice") == "auto"


# ── named tool_choice rejection → same-round auto downgrade ──────────────────
# deepseek thinking mode 400s a NAMED tool_choice ("Thinking mode does not
# support this tool_choice").  The pin must degrade to tool_choice='auto' within
# the same round (once per run) instead of failing the run — but only when
# nothing has streamed to the frontend yet.


def test_inline_run_downgrades_rejected_tool_choice_and_retries(monkeypatch) -> None:
    """First-round named pin is rejected by the provider (error frame mentions
    tool_choice, nothing streamed) → same-round retry with tool_choice='auto' →
    the model calls create_canvas anyway → run COMPLETED, canvas materialized."""
    from agent_session_orchestrator.config import Settings
    from agent_session_orchestrator.services import inline_run_executor as ex

    db, run, install = _canvas_db_and_run(run_id="run_dg")
    install(monkeypatch)

    settings = Settings(
        env="test", otel_enabled=False, redis_url="redis://x",
        llm_gateway_url="http://gw/api/llm-gateway/v1",
        knowledge_ingest_url="", foundation_dlp_url="",
    )
    args = json.dumps({"title": "AI 与就业", "content_markdown": _CANVAS_BODY})
    scripts = [
        # Attempt 1 (named tool_choice): rejected before any content streams.
        [
            _sse(
                {
                    "error": {
                        "message": "Thinking mode does not support this tool_choice",
                        "type": "invalid_request_error",
                        "code": "invalid_request_error",
                    }
                }
            ),
        ],
        # Attempt 2 (auto): the forcing prompt still triggers the tool call.
        _tool_call_frames(call_id="c1", name="create_canvas", arguments=args)
        + [_usage_frame(10, 4, 14)],
        # Round 2: pointer reply.
        [
            _sse({"choices": [{"index": 0, "delta": {"content": "已为你创作《AI 与就业》。"}}]}),
            _sse({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}),
            _usage_frame(20, 5, 25),
            "data: [DONE]",
        ],
    ]
    fake_client = _MultiRoundGatewayClient(sse_scripts=scripts)
    _FakeRedisStream.reset()

    async def _run() -> None:
        with _patch_redis(_FakeRedisStream), \
                patch("httpx.AsyncClient", return_value=fake_client):
            await ex._execute_inline_run(
                settings=settings, tenant_id=TEST_TENANT, user_id=TEST_USER,
                session_id="ses_1", run_id="run_dg", task="写一篇《AI 与就业》短文",
                model_hint="deepseek-reasoner", sys_prompt=None, memory="", history=[],
                sampling_params={},
            )

    asyncio.get_event_loop().run_until_complete(_run())

    assert run.status == "COMPLETED", run.error_message
    # Attempt 1 pinned create_canvas; the retry downgraded to auto.
    assert (
        fake_client.chat_payloads[0]["tool_choice"]["function"]["name"]
        == "create_canvas"
    )
    assert fake_client.chat_payloads[1]["tool_choice"] == "auto"
    # Three requests total: rejected pin + auto retry + pointer-reply round.
    assert len(fake_client.chat_payloads) == 3
    # Canvas still materialized via the retried round.
    assert len(db.canvases) == 1
    assert db.canvases[0].title == "AI 与就业"


def test_inline_run_non_tool_choice_error_is_not_retried(monkeypatch) -> None:
    """A gateway error that does not implicate tool_choice propagates as before:
    no retry, run FAILED, single request."""
    from agent_session_orchestrator.config import Settings
    from agent_session_orchestrator.services import inline_run_executor as ex

    run = Run(
        id="run_nr", tenant_id=TEST_TENANT, session_id="ses_1",
        task_description="写一篇文章", status="RUNNING", execution_mode="inline",
        sandbox_id=None, started_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
        created_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
    )
    _install_fake_db(monkeypatch, run)
    monkeypatch.setattr(ex, "_canvas_enabled_for_tenant", AsyncMock(return_value=True))

    settings = Settings(
        env="test", otel_enabled=False, redis_url="redis://x",
        llm_gateway_url="http://gw/api/llm-gateway/v1",
        knowledge_ingest_url="", foundation_dlp_url="",
    )
    lines = [
        _sse({"error": {"message": "upstream exploded", "code": "PROVIDER.UPSTREAM_ERROR"}}),
    ]
    fake_client = _FakeGatewayClient(sse_lines=lines)
    _FakeRedisStream.reset()

    async def _run() -> None:
        with _patch_redis(_FakeRedisStream), \
                patch("httpx.AsyncClient", return_value=fake_client):
            await ex._execute_inline_run(
                settings=settings, tenant_id=TEST_TENANT, user_id=TEST_USER,
                # Written-creation task → the first round pins tool_choice.
                session_id="ses_1", run_id="run_nr", task="写一篇文章",
                model_hint="deepseek-reasoner", sys_prompt=None, memory="", history=[],
                sampling_params={},
            )

    asyncio.get_event_loop().run_until_complete(_run())

    assert run.status == "FAILED"
    assert "upstream exploded" in (run.error_message or "")
    # No downgrade retry was attempted for a non-tool_choice error.
    assert len(fake_client.chat_payloads) == 1


def test_inline_run_no_downgrade_retry_after_content_streamed(monkeypatch) -> None:
    """A tool_choice error arriving AFTER delta tokens already streamed must not
    retry (a rerun would duplicate frontend content) — the run fails as before."""
    from agent_session_orchestrator.config import Settings
    from agent_session_orchestrator.services import inline_run_executor as ex

    run = Run(
        id="run_late", tenant_id=TEST_TENANT, session_id="ses_1",
        task_description="写一篇文章", status="RUNNING", execution_mode="inline",
        sandbox_id=None, started_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
        created_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
    )
    _install_fake_db(monkeypatch, run)
    monkeypatch.setattr(ex, "_canvas_enabled_for_tenant", AsyncMock(return_value=True))

    settings = Settings(
        env="test", otel_enabled=False, redis_url="redis://x",
        llm_gateway_url="http://gw/api/llm-gateway/v1",
        knowledge_ingest_url="", foundation_dlp_url="",
    )
    lines = [
        # A delta reached the frontend before the provider errored.
        _sse({"choices": [{"index": 0, "delta": {"content": "部分正文"}}]}),
        _sse(
            {
                "error": {
                    "message": "Thinking mode does not support this tool_choice",
                    "type": "invalid_request_error",
                }
            }
        ),
    ]
    fake_client = _FakeGatewayClient(sse_lines=lines)
    _FakeRedisStream.reset()

    async def _run() -> None:
        with _patch_redis(_FakeRedisStream), \
                patch("httpx.AsyncClient", return_value=fake_client):
            await ex._execute_inline_run(
                settings=settings, tenant_id=TEST_TENANT, user_id=TEST_USER,
                session_id="ses_1", run_id="run_late", task="写一篇文章",
                model_hint="deepseek-reasoner", sys_prompt=None, memory="", history=[],
                sampling_params={},
            )

    asyncio.get_event_loop().run_until_complete(_run())

    assert run.status == "FAILED"
    assert "tool_choice" in (run.error_message or "")
    # Content had streamed → no retry despite the tool_choice error text.
    assert len(fake_client.chat_payloads) == 1


# ── deterministic canvas rescue (forced written-creation, no canvas produced) ─
# On weak-instruction models the forcing prompt alone is only ~50% reliable; when
# it misses the model either dumps the whole article into the chat body (hijack) or
# copies the forcing prompt's example sentence and claims a canvas that never
# existed.  The rescue closes both so the user never faces a phantom canvas.


def _long_body_frames(body: str, *, finish: str = "stop") -> list[str]:
    """SSE frames streaming ``body`` as a single content delta, then a finish + usage."""
    return [
        _sse({"choices": [{"index": 0, "delta": {"content": body}}]}),
        _sse({"choices": [{"index": 0, "delta": {}, "finish_reason": finish}]}),
        _usage_frame(20, 300, 320),
        "data: [DONE]",
    ]


def test_inline_rescue_body_as_article_materializes_without_extra_request(monkeypatch) -> None:
    """Forced written-creation + model never calls the tool + a LONG body (>=rescue
    threshold) → the streamed body is materialized as the canvas directly, with NO
    extra gateway request (the reply itself was the article)."""
    from agent_session_orchestrator.config import Settings
    from agent_session_orchestrator.services import inline_run_executor as ex

    db, run, install = _canvas_db_and_run(run_id="run_rescue_body")
    install(monkeypatch)

    settings = Settings(
        env="test", otel_enabled=False, redis_url="redis://x",
        llm_gateway_url="http://gw/api/llm-gateway/v1",
        knowledge_ingest_url="", foundation_dlp_url="",
    )
    # Round 1 (named tool_choice pinned): model ignores the tool and writes the
    # whole article into the body instead.
    scripts = [_long_body_frames(_CANVAS_BODY)]
    fake_client = _MultiRoundGatewayClient(sse_scripts=scripts)
    _FakeRedisStream.reset()

    async def _run() -> None:
        with _patch_redis(_FakeRedisStream), \
                patch("httpx.AsyncClient", return_value=fake_client):
            await ex._execute_inline_run(
                settings=settings, tenant_id=TEST_TENANT, user_id=TEST_USER,
                session_id="ses_1", run_id="run_rescue_body",
                task="写一篇《AI 与就业》短文",
                model_hint="deepseek-chat", sys_prompt=None, memory="", history=[],
                sampling_params={},
            )

    asyncio.get_event_loop().run_until_complete(_run())

    assert run.status == "COMPLETED", run.error_message
    # Exactly one gateway request — the rescue did NOT issue a silent completion.
    assert len(fake_client.chat_payloads) == 1
    # The body was materialized as the canvas, titled from the 《》 in the task.
    assert len(db.canvases) == 1
    assert db.canvases[0].title == "AI 与就业"
    assert db.canvases[0].source_run_id == "run_rescue_body"
    # A ``canvas`` token-stream entry was written (relayed as canvas_created).
    redis_client = _FakeRedisStream.from_url()
    assert len([e for e in redis_client.entries if e[1]["type"] == "canvas"]) == 1
    # The article body still streamed to the frontend as delta tokens.
    deltas = "".join(e[1]["text"] for e in redis_client.entries if e[1]["type"] == "delta")
    assert "人工智能" in deltas


def test_inline_rescue_silent_completion_fetches_full_text(monkeypatch) -> None:
    """Forced written-creation + model never calls the tool + a SHORT body (a
    phantom-canvas claim) → a silent extra completion fetches the full text, which
    is materialized; the silent round NEVER XADDs to the run token stream."""
    from agent_session_orchestrator.config import Settings
    from agent_session_orchestrator.services import inline_run_executor as ex

    db, run, install = _canvas_db_and_run(run_id="run_rescue_lie")
    install(monkeypatch)

    settings = Settings(
        env="test", otel_enabled=False, redis_url="redis://x",
        llm_gateway_url="http://gw/api/llm-gateway/v1",
        knowledge_ingest_url="", foundation_dlp_url="",
    )
    scripts = [
        # Round 1: the model lies — short pointer reply, no tool call.
        _long_body_frames("已为你创作《AI 与就业》，内容在右侧画布中。", finish="stop"),
        # Silent rescue completion: returns the real full article.
        _long_body_frames(_CANVAS_BODY),
    ]
    fake_client = _MultiRoundGatewayClient(sse_scripts=scripts)
    _FakeRedisStream.reset()

    async def _run() -> None:
        with _patch_redis(_FakeRedisStream), \
                patch("httpx.AsyncClient", return_value=fake_client):
            await ex._execute_inline_run(
                settings=settings, tenant_id=TEST_TENANT, user_id=TEST_USER,
                session_id="ses_1", run_id="run_rescue_lie",
                task="写一篇《AI 与就业》短文",
                model_hint="deepseek-chat", sys_prompt=None, memory="", history=[],
                sampling_params={},
            )

    asyncio.get_event_loop().run_until_complete(_run())

    assert run.status == "COMPLETED", run.error_message
    # A silent rescue completion WAS issued (2 requests: original + rescue fetch).
    assert len(fake_client.chat_payloads) == 2
    # The rescue request carried no tools / tool_choice (a plain completion).
    rescue_payload = fake_client.chat_payloads[1]
    assert "tools" not in rescue_payload and "tool_choice" not in rescue_payload
    # It appended a user instruction asking for the full body.
    assert rescue_payload["messages"][-1]["role"] == "user"
    assert "完整正文" in rescue_payload["messages"][-1]["content"]
    # The fetched full text was materialized as the canvas.
    assert len(db.canvases) == 1
    assert db.canvases[0].title == "AI 与就业"
    # Exactly ONE canvas token-stream entry — the materialization; the silent
    # completion added no delta/thinking entries to the run stream.
    redis_client = _FakeRedisStream.from_url()
    assert len([e for e in redis_client.entries if e[1]["type"] == "canvas"]) == 1
    # Only the lie reply reached the frontend as deltas — NOT the full article.
    deltas = "".join(e[1]["text"] for e in redis_client.entries if e[1]["type"] == "delta")
    assert "内容在右侧画布中" in deltas
    assert "人工智能正在深刻改变" not in deltas


def test_inline_rescue_not_triggered_when_tool_already_called(monkeypatch) -> None:
    """When the model already produced a canvas via the tool loop, the rescue does
    NOT fire — no extra gateway request, exactly one canvas."""
    from agent_session_orchestrator.config import Settings
    from agent_session_orchestrator.services import inline_run_executor as ex

    db, run, install = _canvas_db_and_run(run_id="run_rescue_ok")
    install(monkeypatch)

    settings = Settings(
        env="test", otel_enabled=False, redis_url="redis://x",
        llm_gateway_url="http://gw/api/llm-gateway/v1",
        knowledge_ingest_url="", foundation_dlp_url="",
    )
    args = json.dumps({"title": "AI 与就业", "content_markdown": _CANVAS_BODY, "kind": "article"})
    scripts = [
        _tool_call_frames(call_id="call_1", name="create_canvas", arguments=args)
        + [_usage_frame(20, 8, 28)],
        [
            _sse({"choices": [{"index": 0, "delta": {"content": "已为你创作《AI 与就业》。"}}]}),
            _sse({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}),
            _usage_frame(30, 6, 36),
            "data: [DONE]",
        ],
    ]
    fake_client = _MultiRoundGatewayClient(sse_scripts=scripts)
    _FakeRedisStream.reset()

    async def _run() -> None:
        with _patch_redis(_FakeRedisStream), \
                patch("httpx.AsyncClient", return_value=fake_client):
            await ex._execute_inline_run(
                settings=settings, tenant_id=TEST_TENANT, user_id=TEST_USER,
                session_id="ses_1", run_id="run_rescue_ok",
                task="写一篇《AI 与就业》短文",
                model_hint="deepseek-chat", sys_prompt=None, memory="", history=[],
                sampling_params={},
            )

    asyncio.get_event_loop().run_until_complete(_run())

    assert run.status == "COMPLETED", run.error_message
    # Two rounds only (tool call + pointer reply) — no third rescue request.
    assert len(fake_client.chat_payloads) == 2
    assert len(db.canvases) == 1


def test_inline_rescue_not_triggered_for_ordinary_chat(monkeypatch) -> None:
    """A non-written-creation prompt (rescue never forced) that produces no canvas
    and only a short reply is left alone — no rescue, no canvas, no extra request."""
    from agent_session_orchestrator.config import Settings
    from agent_session_orchestrator.services import inline_run_executor as ex

    db, run, install = _canvas_db_and_run(run_id="run_chat")
    # The task is ordinary chat, so force_canvas_first_round is False.
    run.task_description = "今天几号？"
    install(monkeypatch)

    settings = Settings(
        env="test", otel_enabled=False, redis_url="redis://x",
        llm_gateway_url="http://gw/api/llm-gateway/v1",
        knowledge_ingest_url="", foundation_dlp_url="",
    )
    scripts = [
        [
            _sse({"choices": [{"index": 0, "delta": {"content": "今天是7月15日。"}}]}),
            _sse({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}),
            _usage_frame(3, 2, 5),
            "data: [DONE]",
        ],
    ]
    fake_client = _MultiRoundGatewayClient(sse_scripts=scripts)
    _FakeRedisStream.reset()

    async def _run() -> None:
        with _patch_redis(_FakeRedisStream), \
                patch("httpx.AsyncClient", return_value=fake_client):
            await ex._execute_inline_run(
                settings=settings, tenant_id=TEST_TENANT, user_id=TEST_USER,
                session_id="ses_1", run_id="run_chat", task="今天几号？",
                model_hint="deepseek-chat", sys_prompt=None, memory="", history=[],
                sampling_params={},
            )

    asyncio.get_event_loop().run_until_complete(_run())

    assert run.status == "COMPLETED", run.error_message
    # Single request; no rescue for a non-written-creation prompt.
    assert len(fake_client.chat_payloads) == 1
    assert db.canvases == []
    redis_client = _FakeRedisStream.from_url()
    assert not any(e[1]["type"] == "canvas" for e in redis_client.entries)


def test_inline_rescue_abandons_when_fetched_text_too_short(monkeypatch) -> None:
    """Forced written-creation + short reply → silent rescue completion, but the
    fetched text is ALSO too short → materialization is abandoned (no canvas), and
    the run still completes normally."""
    from agent_session_orchestrator.config import Settings
    from agent_session_orchestrator.services import inline_run_executor as ex

    db, run, install = _canvas_db_and_run(run_id="run_rescue_short")
    install(monkeypatch)

    settings = Settings(
        env="test", otel_enabled=False, redis_url="redis://x",
        llm_gateway_url="http://gw/api/llm-gateway/v1",
        knowledge_ingest_url="", foundation_dlp_url="",
    )
    scripts = [
        # Round 1: short lie reply, no tool call.
        _long_body_frames("好的，已经写好放在画布里了。", finish="stop"),
        # Silent rescue completion: still too short to be an article.
        _long_body_frames("抱歉。", finish="stop"),
    ]
    fake_client = _MultiRoundGatewayClient(sse_scripts=scripts)
    _FakeRedisStream.reset()

    async def _run() -> None:
        with _patch_redis(_FakeRedisStream), \
                patch("httpx.AsyncClient", return_value=fake_client):
            await ex._execute_inline_run(
                settings=settings, tenant_id=TEST_TENANT, user_id=TEST_USER,
                session_id="ses_1", run_id="run_rescue_short",
                task="写一篇《AI 与就业》短文",
                model_hint="deepseek-chat", sys_prompt=None, memory="", history=[],
                sampling_params={},
            )

    asyncio.get_event_loop().run_until_complete(_run())

    # Run still completes; the rescue completion was issued but produced nothing
    # materializable, so no canvas exists and no canvas SSE was emitted.
    assert run.status == "COMPLETED", run.error_message
    assert len(fake_client.chat_payloads) == 2  # original + rescue fetch
    assert db.canvases == []
    redis_client = _FakeRedisStream.from_url()
    assert not any(e[1]["type"] == "canvas" for e in redis_client.entries)


def test_extract_creation_title_prefers_book_title_then_truncates() -> None:
    from agent_session_orchestrator.services.canvas_tool_spec import extract_creation_title

    # 《书名号》 wins.
    assert extract_creation_title("写一篇《AI 与就业》短文，约300字") == "AI 与就业"
    # No 《》 → whitespace-collapsed prompt, truncated to 30 chars.
    long_task = "帮我写一篇关于人工智能如何改变现代教育体系与终身学习方式的深度长文章内容要详实"
    title = extract_creation_title(long_task)
    assert len(title) == 30 and title == long_task[:30]
    # Blank → a non-empty placeholder (rescue always has a title).
    assert extract_creation_title("") == "未命名"
    assert extract_creation_title("   ") == "未命名"


# ─────────────────────────────────────────────────────────────────────────────
# Inline skill support — catalog prompt, read_skill tool + loop, presence semantics
# ─────────────────────────────────────────────────────────────────────────────


def _skill_settings():
    """Settings with canvas OFF and a skill-store URL — isolates the skill path."""
    from agent_session_orchestrator.config import Settings

    return Settings(
        env="test", otel_enabled=False, redis_url="redis://x",
        llm_gateway_url="http://gw/api/llm-gateway/v1",
        knowledge_ingest_url="", foundation_dlp_url="",
        skill_store_url="http://skill-store/api/skills/v1",
    )


def _mock_resolve(monkeypatch, entries: list[dict]) -> list[list[str]]:
    """Patch the loader's ``resolve_skills`` to return ``entries``; record calls.

    Returns a list that captures each call's ``names`` argument so a test can
    assert what the executor asked to resolve (presence semantics).
    """
    from agent_session_orchestrator.services import inline_run_executor as ex

    calls: list[list[str]] = []

    async def _fake_resolve(names, **kw):
        calls.append(list(names) if names else [])
        return list(entries)

    monkeypatch.setattr(ex.inline_skill_loader, "resolve_skills", _fake_resolve)
    return calls


def test_inline_skills_explicit_advertises_read_skill_and_catalog(monkeypatch) -> None:
    """Explicit skills → the resolved catalog appears in the system prompt and the
    request advertises tools=[read_skill] (canvas OFF, so read_skill is the only
    tool)."""
    from agent_session_orchestrator.services import inline_run_executor as ex

    run = Run(
        id="run_sk1", tenant_id=TEST_TENANT, session_id="ses_1",
        task_description="用 pandas 分析数据", status="RUNNING", execution_mode="inline",
        sandbox_id=None, started_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
        created_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
    )
    _install_fake_db(monkeypatch, run)
    monkeypatch.setattr(ex, "_canvas_enabled_for_tenant", AsyncMock(return_value=False))
    _mock_resolve(
        monkeypatch,
        [{"name": "data-analyzer", "version": "1", "sha256": "a" * 64,
          "description": "分析表格数据并给出洞察"}],
    )

    lines = [
        _sse({"choices": [{"index": 0, "delta": {"content": "好的"}}]}),
        _usage_frame(3, 2, 5),
        "data: [DONE]",
    ]
    fake_client = _FakeGatewayClient(sse_lines=lines)
    _FakeRedisStream.reset()

    async def _run() -> None:
        with _patch_redis(_FakeRedisStream), \
                patch("httpx.AsyncClient", return_value=fake_client):
            await ex._execute_inline_run(
                settings=_skill_settings(), tenant_id=TEST_TENANT, user_id=TEST_USER,
                session_id="ses_1", run_id="run_sk1", task="用 pandas 分析数据",
                model_hint="deepseek-chat", sys_prompt=None, memory="", history=[],
                sampling_params={}, skills=["data-analyzer"],
            )

    asyncio.get_event_loop().run_until_complete(_run())

    assert run.status == "COMPLETED"
    payload = fake_client.chat_payloads[0]
    # read_skill advertised; create_canvas is NOT (canvas off).
    tool_names = [t["function"]["name"] for t in payload.get("tools", [])]
    assert tool_names == ["read_skill"]
    # The catalog block lists the skill (name + description) in the system prompt.
    system_msg = payload["messages"][0]
    assert system_msg["role"] == "system"
    assert "data-analyzer" in system_msg["content"]
    assert "分析表格数据并给出洞察" in system_msg["content"]
    assert "read_skill" in system_msg["content"]
    # The inline-mode script boundary is stated.
    assert "无法执行脚本" in system_msg["content"]


def test_inline_skills_omitted_uses_defaults_explicit_empty_disables(monkeypatch) -> None:
    """skills=None (omitted at the endpoint) → whatever resolve returns for the
    default set; skills=[] (explicit empty) → no catalog, no read_skill tool."""
    from agent_session_orchestrator.services import inline_run_executor as ex

    # Case A: skills omitted (None) — the executor still calls resolve; here the
    # default set resolves to one skill, so read_skill IS advertised.
    run_a = Run(
        id="run_def", tenant_id=TEST_TENANT, session_id="ses_1",
        task_description="hi", status="RUNNING", execution_mode="inline",
        sandbox_id=None, started_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
        created_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
    )
    _install_fake_db(monkeypatch, run_a)
    monkeypatch.setattr(ex, "_canvas_enabled_for_tenant", AsyncMock(return_value=False))
    calls = _mock_resolve(
        monkeypatch,
        [{"name": "default-skill", "version": "1", "sha256": "b" * 64,
          "description": "默认技能"}],
    )
    lines = [
        _sse({"choices": [{"index": 0, "delta": {"content": "ok"}}]}),
        _usage_frame(1, 1, 2), "data: [DONE]",
    ]
    fake_a = _FakeGatewayClient(sse_lines=lines)
    _FakeRedisStream.reset()

    async def _run_a() -> None:
        with _patch_redis(_FakeRedisStream), \
                patch("httpx.AsyncClient", return_value=fake_a):
            await ex._execute_inline_run(
                settings=_skill_settings(), tenant_id=TEST_TENANT, user_id=TEST_USER,
                session_id="ses_1", run_id="run_def", task="hi",
                model_hint="deepseek-chat", sys_prompt=None, memory="", history=[],
                sampling_params={}, skills=None,
            )

    asyncio.get_event_loop().run_until_complete(_run_a())
    assert [t["function"]["name"] for t in fake_a.chat_payloads[0].get("tools", [])] \
        == ["read_skill"]

    # Case B: skills=[] (explicit empty) — resolve returns nothing → no read_skill,
    # no catalog block in the system prompt.
    run_b = Run(
        id="run_none", tenant_id=TEST_TENANT, session_id="ses_1",
        task_description="hi", status="RUNNING", execution_mode="inline",
        sandbox_id=None, started_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
        created_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
    )
    _install_fake_db(monkeypatch, run_b)
    _mock_resolve(monkeypatch, [])  # empty catalog for any input
    fake_b = _FakeGatewayClient(sse_lines=lines)
    _FakeRedisStream.reset()

    async def _run_b() -> None:
        with _patch_redis(_FakeRedisStream), \
                patch("httpx.AsyncClient", return_value=fake_b):
            await ex._execute_inline_run(
                settings=_skill_settings(), tenant_id=TEST_TENANT, user_id=TEST_USER,
                session_id="ses_1", run_id="run_none", task="hi",
                model_hint="deepseek-chat", sys_prompt=None, memory="", history=[],
                sampling_params={}, skills=[],
            )

    asyncio.get_event_loop().run_until_complete(_run_b())
    payload_b = fake_b.chat_payloads[0]
    assert "tools" not in payload_b  # no canvas, no read_skill → plain completion
    # No catalog block: system message (if any) carries no skill listing.
    system_b = payload_b["messages"][0]
    if system_b.get("role") == "system":
        assert "read_skill" not in system_b["content"]
    assert calls  # resolve was called in case A (presence semantics honoured)


def test_inline_read_skill_tool_call_round_trip(monkeypatch) -> None:
    """Round 1: model calls read_skill → the tool result frame carries the fetched
    SKILL.md body; round 2: the model answers normally with the loaded instructions."""
    from agent_session_orchestrator.services import inline_run_executor as ex

    run = Run(
        id="run_rs", tenant_id=TEST_TENANT, session_id="ses_1",
        task_description="做数据分析", status="RUNNING", execution_mode="inline",
        sandbox_id=None, started_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
        created_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
    )
    _install_fake_db(monkeypatch, run)
    monkeypatch.setattr(ex, "_canvas_enabled_for_tenant", AsyncMock(return_value=False))
    _mock_resolve(
        monkeypatch,
        [{"name": "data-analyzer", "version": "1", "sha256": "c" * 64,
          "description": "分析数据"}],
    )

    skill_body = "# Data Analyzer\n\n先加载 CSV，再用 pandas 计算描述性统计，最后给出结论。"
    fetch_calls: list[dict] = []

    async def _fake_fetch(name, **kw):
        fetch_calls.append({"name": name, **kw})
        return skill_body

    monkeypatch.setattr(ex.inline_skill_loader, "fetch_skill_file", _fake_fetch)

    args = json.dumps({"name": "data-analyzer", "path": "SKILL.md"})
    scripts = [
        # Round 1: the model calls read_skill.
        _tool_call_frames(call_id="rs_1", name="read_skill", arguments=args)
        + [_usage_frame(10, 4, 14)],
        # Round 2: after the tool result, the model answers using the instructions.
        [
            _sse({"choices": [{"index": 0, "delta": {"content": "我已按技能指令完成分析。"}}]}),
            _sse({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}),
            _usage_frame(20, 6, 26), "data: [DONE]",
        ],
    ]
    fake_client = _MultiRoundGatewayClient(sse_scripts=scripts)
    _FakeRedisStream.reset()

    async def _run() -> None:
        with _patch_redis(_FakeRedisStream), \
                patch("httpx.AsyncClient", return_value=fake_client):
            await ex._execute_inline_run(
                settings=_skill_settings(), tenant_id=TEST_TENANT, user_id=TEST_USER,
                session_id="ses_1", run_id="run_rs", task="做数据分析",
                model_hint="deepseek-chat", sys_prompt=None, memory="", history=[],
                sampling_params={}, skills=["data-analyzer"],
            )

    asyncio.get_event_loop().run_until_complete(_run())

    assert run.status == "COMPLETED"
    # The loader was asked for the requested skill, with the resolved sha for caching.
    assert fetch_calls and fetch_calls[0]["name"] == "data-analyzer"
    assert fetch_calls[0]["path"] == "SKILL.md"
    assert fetch_calls[0]["sha256"] == "c" * 64
    # Round 2 saw the assistant tool_calls frame + a tool result frame whose
    # content is the fetched SKILL.md body (OpenAI ordering preserved).
    round2_msgs = fake_client.chat_payloads[1]["messages"]
    assistant_frame = next(
        m for m in round2_msgs if m.get("role") == "assistant" and m.get("tool_calls")
    )
    tool_frame = next(m for m in round2_msgs if m.get("role") == "tool")
    assert tool_frame["content"] == skill_body
    assert tool_frame["tool_call_id"] == "rs_1"
    # The tool frame follows its assistant tool_calls frame.
    assert round2_msgs.index(assistant_frame) < round2_msgs.index(tool_frame)
    # The final reply streamed to the frontend.
    redis_client = _FakeRedisStream.from_url()
    deltas = "".join(e[1]["text"] for e in redis_client.entries if e[1]["type"] == "delta")
    assert "已按技能指令完成分析" in deltas


def test_inline_mcp_tool_call_round_trip(monkeypatch) -> None:
    """Inline chat advertises discovered MCP tools, executes tools/call, emits
    visible tool events, and feeds the result into the next completion round."""
    from agent_session_orchestrator.services import inline_run_executor as ex

    exposed_name = "mcp__echo__echo"
    run = Run(
        id="run_mcp", tenant_id=TEST_TENANT, session_id="ses_1",
        task_description="用 MCP 回显你好", status="RUNNING", execution_mode="inline",
        sandbox_id=None, started_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
        created_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
    )
    _install_fake_db(monkeypatch, run)
    monkeypatch.setattr(ex, "_canvas_enabled_for_tenant", AsyncMock(return_value=False))
    monkeypatch.setattr(ex.inline_skill_loader, "resolve_skills", AsyncMock(return_value=[]))

    class _FakeMcpCatalog:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict]] = []
            self.closed = False

        @property
        def openai_tools(self):
            return [{
                "type": "function",
                "function": {
                    "name": exposed_name,
                    "description": "[echo] Echo input text",
                    "parameters": {
                        "type": "object",
                        "properties": {"text": {"type": "string"}},
                        "required": ["text"],
                    },
                },
            }]

        def has(self, name):
            return name == exposed_name

        async def call(self, name, args):
            self.calls.append((name, args))
            return "ECHO: 你好"

        async def close(self):
            self.closed = True

    catalog = _FakeMcpCatalog()
    load_catalog = AsyncMock(return_value=catalog)
    monkeypatch.setattr(ex.inline_mcp_client, "load_inline_mcp_catalog", load_catalog)

    scripts = [
        _tool_call_frames(
            call_id="mcp_1",
            name=exposed_name,
            arguments=json.dumps({"text": "你好"}, ensure_ascii=False),
        ) + [_usage_frame(10, 4, 14)],
        [
            _sse({"choices": [{"index": 0, "delta": {"content": "MCP 返回了：你好。"}}]}),
            _sse({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}),
            _usage_frame(20, 6, 26), "data: [DONE]",
        ],
    ]
    fake_client = _MultiRoundGatewayClient(sse_scripts=scripts)
    _FakeRedisStream.reset()

    async def _run() -> None:
        with _patch_redis(_FakeRedisStream), \
                patch("httpx.AsyncClient", return_value=fake_client):
            await ex._execute_inline_run(
                settings=_skill_settings(), tenant_id=TEST_TENANT, user_id=TEST_USER,
                session_id="ses_1", run_id="run_mcp", task="用 MCP 回显你好",
                model_hint="deepseek-chat", sys_prompt=None, memory="", history=[],
                sampling_params={},
            )

    asyncio.get_event_loop().run_until_complete(_run())

    assert run.status == "COMPLETED"
    assert catalog.calls == [(exposed_name, {"text": "你好"})]
    assert catalog.closed is True
    assert load_catalog.await_args.kwargs["tenant_id"] == TEST_TENANT
    assert fake_client.chat_payloads[0]["tools"][-1]["function"]["name"] == exposed_name
    round2_tool = next(
        message for message in fake_client.chat_payloads[1]["messages"]
        if message.get("role") == "tool"
    )
    assert round2_tool["content"] == "ECHO: 你好"

    entries = [entry for _eid, entry in _FakeRedisStream.from_url().entries]
    call_entry = next(entry for entry in entries if entry["type"] == "tool_call")
    result_entry = next(entry for entry in entries if entry["type"] == "tool_result")
    assert call_entry["name"] == exposed_name
    assert result_entry["result"] == "ECHO: 你好"
    assert result_entry["state"] == "success"


def test_inline_mcp_tool_call_respects_explicit_dlp_deny(monkeypatch) -> None:
    """A successful non-ALLOW DLP verdict blocks MCP execution; outages remain
    fail-open in the helper and are covered by its implementation contract."""
    from agent_session_orchestrator.services import inline_run_executor as ex

    class _DlpClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, **kwargs):
            assert url.endswith("/core/scan")
            return _JsonResp(200, {"action": "BLOCK", "max_sensitivity": "L4"})

    async def _run():
        with patch("httpx.AsyncClient", return_value=_DlpClient()):
            return await ex._inline_tool_call_dlp_decision(
                dlp_url="http://dlp/api/dlp/v1",
                tenant_id=TEST_TENANT,
                user_id=TEST_USER,
                run_id="run_mcp",
                tool_call_id="call_1",
                tool_name="mcp__crm__delete_customer",
                arguments={"customer_id": "secret"},
            )

    allowed, reason = asyncio.get_event_loop().run_until_complete(_run())
    assert allowed is False
    assert "DLP 已阻止" in reason
    assert "L4" in reason


def test_inline_resolve_skips_failed_skill_fail_open() -> None:
    """resolve_skills drops a skill that fails to resolve and keeps the healthy
    one — a single bad skill never fails the batch (unit test of the loader)."""
    from agent_session_orchestrator.services import inline_skill_loader as loader

    class _Resp:
        def __init__(self, status_code, payload):
            self.status_code = status_code
            self._payload = payload

        def json(self):
            return self._payload

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def get(self, url, params=None, headers=None):
            name = (params or {}).get("names")
            if name == "good":
                return _Resp(200, {"items": [{
                    "name": "good", "version": "1", "sha256": "d" * 64,
                    "description": "健康技能",
                }]})
            # The bad skill 500s — must be skipped, not raised.
            return _Resp(500, {})

    async def _run():
        with patch("httpx.AsyncClient", return_value=_Client()):
            return await loader.resolve_skills(
                ["good", "bad"],
                skill_store_url="http://skill-store/api/skills/v1",
                service_scopes="skill.read_internal",
                tenant_id=TEST_TENANT, user_id=TEST_USER,
            )

    entries = asyncio.get_event_loop().run_until_complete(_run())
    assert [e["name"] for e in entries] == ["good"]
    assert entries[0]["description"] == "健康技能"


def test_inline_fetch_skill_file_truncates_over_64kb() -> None:
    """A skill file body over 64KB is truncated with the Chinese truncation notice
    (unit test of the loader)."""
    from agent_session_orchestrator.services import inline_skill_loader as loader

    big = "字" * 40000  # ~120KB UTF-8, well over the 64KB cap

    class _Resp:
        status_code = 200

        def json(self):
            return {"name": "big", "version": "1", "sha256": "e" * 64,
                    "path": "SKILL.md", "content": big}

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def get(self, url, params=None, headers=None):
            return _Resp()

    async def _run():
        with patch("httpx.AsyncClient", return_value=_Client()):
            return await loader.fetch_skill_file(
                "big",
                skill_store_url="http://skill-store/api/skills/v1",
                service_scopes="skill.read_internal",
                tenant_id=TEST_TENANT, user_id=TEST_USER,
                sha256="e" * 64, path="SKILL.md",
            )

    body = asyncio.get_event_loop().run_until_complete(_run())
    # Truncated below the original length, capped near 64KB, with the notice.
    assert len(body.encode("utf-8")) <= loader._SKILL_FILE_MAX_BYTES + len(
        loader._TRUNCATION_NOTICE.encode("utf-8")
    )
    assert body.endswith(loader._TRUNCATION_NOTICE)
    assert len(body) < len(big)


def test_inline_fetch_skill_file_wire_contract() -> None:
    """Pin the skill-store file-endpoint wire contract: the request must carry
    ``tenant_id``/``user_id``/``path`` as query params (the internal endpoint
    declares them as required query params — omitting them is a 422 on the real
    stack, invisible to loader-level mocks)."""
    from agent_session_orchestrator.services import inline_skill_loader as loader

    seen: dict = {}

    class _Resp:
        status_code = 200

        def json(self):
            return {"name": "wire", "version": "1", "sha256": "f" * 64,
                    "path": "references/notes.md", "content": "ok"}

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def get(self, url, params=None, headers=None):
            seen["url"] = url
            seen["params"] = params
            seen["headers"] = headers
            return _Resp()

    async def _run():
        with patch("httpx.AsyncClient", return_value=_Client()):
            return await loader.fetch_skill_file(
                "wire",
                skill_store_url="http://skill-store/api/skills/v1",
                service_scopes="skill.read_internal",
                tenant_id=TEST_TENANT, user_id=TEST_USER,
                sha256="f" * 64, path="references/notes.md",
            )

    body = asyncio.get_event_loop().run_until_complete(_run())
    assert body == "ok"
    assert seen["url"].endswith("/internal/skills/wire/file")
    assert seen["params"] == {
        "tenant_id": TEST_TENANT,
        "user_id": TEST_USER,
        "path": "references/notes.md",
    }
    assert seen["headers"]["X-Tenant-Id"] == TEST_TENANT
    assert seen["headers"]["X-Service-Scopes"] == "skill.read_internal"


def test_create_run_inline_branch_threads_skills(
    app_client: TestClient, mock_db: AsyncMock, auth_headers,
) -> None:
    """The create_run inline branch threads the presence-resolved skill list into
    start_inline_run (explicit selection wins, defaults are not fetched)."""
    session = Session(id="ses_sk", tenant_id=TEST_TENANT, user_id="u1", status="ACTIVE")
    mock_db.execute.return_value = make_scalar_result(session)

    async def _refresh(obj):
        if isinstance(obj, Run):
            obj.id = "run_sk"
            obj.created_at = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)

    mock_db.refresh.side_effect = _refresh

    started: list[dict] = []
    default_lookups: list[str] = []

    class _FakeClient(AuthzAwareClient):
        async def get(self, url, **kw):
            if "default-installed" in url:
                default_lookups.append(url)
                return __import__("httpx").Response(200, json={"items": []})
            return __import__("httpx").Response(200, json={"hits": []})

        async def post(self, url, json=None, **kw):
            if "/check" in url and "authz" in url:
                return self._authz_allowed_resp()
            if url.endswith("/quota/check"):
                return __import__("httpx").Response(200, json={"allowed": True})
            if url.endswith("/quota/usage"):
                return __import__("httpx").Response(200, json={})
            return __import__("httpx").Response(201, json={"sandbox_id": "x"})

    def _fake_start(**kw):
        started.append(kw)

    import httpx
    with patch.object(httpx, "AsyncClient", return_value=_FakeClient()), \
            patch(
                "agent_session_orchestrator.api.v1.runs.start_inline_run",
                new=_fake_start,
            ):
        resp = app_client.post(
            "/api/session/v1/sessions/ses_sk/runs",
            headers=auth_headers,
            json={"task": "跑技能", "execution_mode": "inline",
                  "skills": ["data-analyzer", "writer"]},
        )

    assert resp.status_code == 201, resp.text
    # Explicit skills threaded verbatim; defaults NOT looked up (presence semantics).
    assert started and started[0]["skills"] == ["data-analyzer", "writer"]
    assert default_lookups == []


# ─────────────────────────────────────────────────────────────────────────────
# Inline attachment reading — DB-queried input files injected into extra_system
# ─────────────────────────────────────────────────────────────────────────────


class _FakeAttachment:
    def __init__(
        self, *, filename: str, mime: str, blob_key: str, id: str = "",
    ) -> None:
        self.id = id
        self.filename = filename
        self.mime = mime
        self.blob_key = blob_key


def _install_fake_db_with_attachments(monkeypatch, run, attachments):
    """Like ``_install_fake_db`` but the fake svc also serves ``list_attachments``.

    ``attachments`` maps to the ``kind='input'`` rows the executor queries.
    """
    from agent_session_orchestrator.services import inline_run_executor as ex

    async def _fake_acquire(*, tenant_id, user_id="SYSTEM", actor="USER"):
        yield object()

    class _FakeSvc:
        def __init__(self, db, *, tenant_id):
            pass

        async def list_runs(self, session_id):
            return [run]

        async def list_messages(self, session_id, *, cursor=None, limit=200):
            return [], None

        async def create_message(self, *, session_id, role, content, tool_calls=None, run_id=None):
            m = MagicMock()
            m.id = "msg_1"
            return m

        async def list_attachments(self, session_id, *, kind=None):
            assert kind == "input"
            return list(attachments)

    monkeypatch.setattr(ex, "acquire_db_session", _fake_acquire)
    monkeypatch.setattr(ex, "SessionService", _FakeSvc)


class _FakeBlobStore:
    def __init__(self, blobs: dict[str, bytes]) -> None:
        self._blobs = blobs

    def get(self, key: str) -> bytes:
        return self._blobs[key]


def _attachment_settings():
    from agent_session_orchestrator.config import Settings

    return Settings(
        env="test", otel_enabled=False, redis_url="redis://x",
        llm_gateway_url="http://gw/api/llm-gateway/v1",
        knowledge_ingest_url="", foundation_dlp_url="",
    )


def test_inline_attachments_injected_into_system_prompt(monkeypatch) -> None:
    """A session with an input attachment → its extracted text lands in the
    system message as an ``<attachment>`` block (canvas + skills off)."""
    from agent_session_orchestrator.services import inline_run_executor as ex

    run = Run(
        id="run_att", tenant_id=TEST_TENANT, session_id="ses_1",
        task_description="总结上传的文件", status="RUNNING", execution_mode="inline",
        sandbox_id=None, started_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
        created_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
    )
    _install_fake_db_with_attachments(
        monkeypatch, run,
        [_FakeAttachment(
            id="att_notes", filename="notes.txt", mime="text/plain", blob_key="k/notes",
        )],
    )
    monkeypatch.setattr(ex, "_canvas_enabled_for_tenant", AsyncMock(return_value=False))
    monkeypatch.setattr(
        ex, "_inline_attachments_enabled_for_tenant", AsyncMock(return_value=True),
    )
    monkeypatch.setattr(ex.inline_skill_loader, "resolve_skills", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        ex, "_make_blob_store",
        lambda settings: _FakeBlobStore({"k/notes": "报告要点：营收增长20%".encode()}),
    )

    lines = [
        _sse({"choices": [{"index": 0, "delta": {"content": "好的"}}]}),
        _usage_frame(3, 2, 5), "data: [DONE]",
    ]
    fake_client = _FakeGatewayClient(sse_lines=lines)
    _FakeRedisStream.reset()

    async def _run() -> None:
        with _patch_redis(_FakeRedisStream), \
                patch("httpx.AsyncClient", return_value=fake_client):
            await ex._execute_inline_run(
                settings=_attachment_settings(), tenant_id=TEST_TENANT, user_id=TEST_USER,
                session_id="ses_1", run_id="run_att", task="总结上传的文件",
                model_hint="deepseek-chat", sys_prompt=None, memory="", history=[],
                sampling_params={}, skills=[],
            )

    asyncio.get_event_loop().run_until_complete(_run())

    assert run.status == "COMPLETED"
    system_msg = fake_client.chat_payloads[0]["messages"][0]
    assert system_msg["role"] == "system"
    assert "用户在本会话上传了以下文件" in system_msg["content"]
    # The attachment id is surfaced as an id attribute on the tag.
    assert '<attachment id="att_notes" filename="notes.txt">' in system_msg["content"]
    assert "报告要点：营收增长20%" in system_msg["content"]
    # A file block was injected → the file-link convention rides along once.
    assert "#file:文件id" in system_msg["content"]
    assert system_msg["content"].count("[文件名](#file:文件id)") == 1


def test_inline_no_attachments_no_injection(monkeypatch) -> None:
    """No input attachments → no system message at all (behaviour unchanged from
    before the feature: canvas + skills off means no extra_system)."""
    from agent_session_orchestrator.services import inline_run_executor as ex

    run = Run(
        id="run_noa", tenant_id=TEST_TENANT, session_id="ses_1",
        task_description="你好", status="RUNNING", execution_mode="inline",
        sandbox_id=None, started_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
        created_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
    )
    _install_fake_db_with_attachments(monkeypatch, run, [])  # zero attachments
    monkeypatch.setattr(ex, "_canvas_enabled_for_tenant", AsyncMock(return_value=False))
    monkeypatch.setattr(
        ex, "_inline_attachments_enabled_for_tenant", AsyncMock(return_value=True),
    )
    monkeypatch.setattr(ex.inline_skill_loader, "resolve_skills", AsyncMock(return_value=[]))

    lines = [
        _sse({"choices": [{"index": 0, "delta": {"content": "你好"}}]}),
        _usage_frame(1, 1, 2), "data: [DONE]",
    ]
    fake_client = _FakeGatewayClient(sse_lines=lines)
    _FakeRedisStream.reset()

    async def _run() -> None:
        with _patch_redis(_FakeRedisStream), \
                patch("httpx.AsyncClient", return_value=fake_client):
            await ex._execute_inline_run(
                settings=_attachment_settings(), tenant_id=TEST_TENANT, user_id=TEST_USER,
                session_id="ses_1", run_id="run_noa", task="你好",
                model_hint="deepseek-chat", sys_prompt=None, memory="", history=[],
                sampling_params={}, skills=[],
            )

    asyncio.get_event_loop().run_until_complete(_run())

    assert run.status == "COMPLETED"
    messages = fake_client.chat_payloads[0]["messages"]
    # No system message injected (first message is the user task).
    assert all(m["role"] != "system" for m in messages)


def test_inline_attachments_flag_off_skips_injection(monkeypatch) -> None:
    """Flag OFF → the attachment reading path is skipped entirely: no DB lookup,
    no blob fetch, no system block (even though an attachment exists)."""
    from agent_session_orchestrator.services import inline_run_executor as ex

    run = Run(
        id="run_off", tenant_id=TEST_TENANT, session_id="ses_1",
        task_description="总结文件", status="RUNNING", execution_mode="inline",
        sandbox_id=None, started_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
        created_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
    )

    listed: list[str] = []

    async def _fake_acquire(*, tenant_id, user_id="SYSTEM", actor="USER"):
        yield object()

    class _FakeSvc:
        def __init__(self, db, *, tenant_id):
            pass

        async def list_runs(self, session_id):
            return [run]

        async def list_messages(self, session_id, *, cursor=None, limit=200):
            return [], None

        async def create_message(self, **kw):
            m = MagicMock()
            m.id = "m1"
            return m

        async def list_attachments(self, session_id, *, kind=None):
            listed.append(session_id)  # must NOT be called when flag is off
            return []

    monkeypatch.setattr(ex, "acquire_db_session", _fake_acquire)
    monkeypatch.setattr(ex, "SessionService", _FakeSvc)
    monkeypatch.setattr(ex, "_canvas_enabled_for_tenant", AsyncMock(return_value=False))
    monkeypatch.setattr(
        ex, "_inline_attachments_enabled_for_tenant", AsyncMock(return_value=False),
    )
    monkeypatch.setattr(ex.inline_skill_loader, "resolve_skills", AsyncMock(return_value=[]))

    lines = [
        _sse({"choices": [{"index": 0, "delta": {"content": "ok"}}]}),
        _usage_frame(1, 1, 2), "data: [DONE]",
    ]
    fake_client = _FakeGatewayClient(sse_lines=lines)
    _FakeRedisStream.reset()

    async def _run() -> None:
        with _patch_redis(_FakeRedisStream), \
                patch("httpx.AsyncClient", return_value=fake_client):
            await ex._execute_inline_run(
                settings=_attachment_settings(), tenant_id=TEST_TENANT, user_id=TEST_USER,
                session_id="ses_1", run_id="run_off", task="总结文件",
                model_hint="deepseek-chat", sys_prompt=None, memory="", history=[],
                sampling_params={}, skills=[],
            )

    asyncio.get_event_loop().run_until_complete(_run())

    assert run.status == "COMPLETED"
    # The attachment-listing path was never entered.
    assert listed == []
    messages = fake_client.chat_payloads[0]["messages"]
    assert all(m["role"] != "system" for m in messages)


def test_rich_converter_is_used_for_word_attachments(monkeypatch) -> None:
    """Configured Word/PDF files use structure-preserving Markdown conversion."""
    from agent_session_orchestrator.services import inline_run_executor as ex

    attachment = _FakeAttachment(
        filename="report.docx",
        mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        blob_key="k/report",
    )
    _install_fake_db_with_attachments(monkeypatch, MagicMock(), [attachment])
    monkeypatch.setattr(
        ex, "_make_blob_store", lambda settings: _FakeBlobStore({"k/report": b"docx"})
    )
    converted = AsyncMock(return_value="# 报告\n\n公式：$E=mc^2$\n\n| A | B |")
    monkeypatch.setattr(ex.doc_to_markdown_client, "convert", converted)
    settings = _attachment_settings().model_copy(
        update={"doc_to_md_url": "http://doc-to-md:8000", "doc_to_md_timeout": 45.0}
    )

    block = asyncio.get_event_loop().run_until_complete(
        ex._build_inline_attachments_prompt(
            settings=settings,
            tenant_id=TEST_TENANT,
            user_id=TEST_USER,
            session_id="ses_1",
        )
    )

    assert block is not None
    assert "公式：$E=mc^2$" in block
    assert "| A | B |" in block
    converted.assert_awaited_once_with(
        base_url="http://doc-to-md:8000",
        filename="report.docx",
        mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        data=b"docx",
        timeout=45.0,
    )


def test_rich_converter_failure_falls_back_to_local_extractor(monkeypatch) -> None:
    """Converter downtime does not fail the run or discard readable content."""
    from agent_session_orchestrator.services import inline_run_executor as ex

    attachment = _FakeAttachment(
        filename="report.pdf", mime="application/pdf", blob_key="k/report"
    )
    _install_fake_db_with_attachments(monkeypatch, MagicMock(), [attachment])
    monkeypatch.setattr(
        ex, "_make_blob_store", lambda settings: _FakeBlobStore({"k/report": b"pdf"})
    )
    monkeypatch.setattr(
        ex.doc_to_markdown_client,
        "convert",
        AsyncMock(side_effect=ex.doc_to_markdown_client.ConversionError("offline")),
    )
    local_extract = MagicMock(return_value="本地回退文本")
    monkeypatch.setattr(ex.attachment_text_extractor, "extract_text", local_extract)
    settings = _attachment_settings().model_copy(
        update={"doc_to_md_url": "http://doc-to-md:8000"}
    )

    block = asyncio.get_event_loop().run_until_complete(
        ex._build_inline_attachments_prompt(
            settings=settings,
            tenant_id=TEST_TENANT,
            user_id=TEST_USER,
            session_id="ses_1",
        )
    )

    assert block is not None
    assert "本地回退文本" in block
    local_extract.assert_called_once_with("report.pdf", "application/pdf", b"pdf")


# ─────────────────────────────────────────────────────────────────────────────
# Workspace context injection — instructions + base files into extra_system
# ─────────────────────────────────────────────────────────────────────────────


def test_inline_workspace_instructions_injected(monkeypatch) -> None:
    """``workspace_instructions`` lands in the system message as a
    ``# 工作区指令`` block (canvas + skills + attachments all off)."""
    from agent_session_orchestrator.services import inline_run_executor as ex

    run = Run(
        id="run_wsi", tenant_id=TEST_TENANT, session_id="ses_1",
        task_description="写周报", status="RUNNING", execution_mode="inline",
        sandbox_id=None, started_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
        created_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
    )
    _install_fake_db_with_attachments(monkeypatch, run, [])
    monkeypatch.setattr(ex, "_canvas_enabled_for_tenant", AsyncMock(return_value=False))
    monkeypatch.setattr(
        ex, "_inline_attachments_enabled_for_tenant", AsyncMock(return_value=True),
    )
    monkeypatch.setattr(ex.inline_skill_loader, "resolve_skills", AsyncMock(return_value=[]))

    lines = [
        _sse({"choices": [{"index": 0, "delta": {"content": "好的"}}]}),
        _usage_frame(3, 2, 5), "data: [DONE]",
    ]
    fake_client = _FakeGatewayClient(sse_lines=lines)
    _FakeRedisStream.reset()

    async def _run() -> None:
        with _patch_redis(_FakeRedisStream), \
                patch("httpx.AsyncClient", return_value=fake_client):
            await ex._execute_inline_run(
                settings=_attachment_settings(), tenant_id=TEST_TENANT, user_id=TEST_USER,
                session_id="ses_1", run_id="run_wsi", task="写周报",
                model_hint="deepseek-chat", sys_prompt=None, memory="", history=[],
                sampling_params={}, skills=[],
                workspace_instructions="所有回复必须使用正式书面语。",
            )

    asyncio.get_event_loop().run_until_complete(_run())

    assert run.status == "COMPLETED"
    system_msg = fake_client.chat_payloads[0]["messages"][0]
    assert system_msg["role"] == "system"
    assert "# 工作区指令" in system_msg["content"]
    assert "所有回复必须使用正式书面语。" in system_msg["content"]


def test_inline_workspace_files_injected_before_attachments(monkeypatch) -> None:
    """Workspace base files are injected as ``<workspace_file>`` blocks, placed
    before the session ``<attachment>`` block; instructions precede both."""
    from agent_session_orchestrator.services import inline_run_executor as ex

    run = Run(
        id="run_wsf", tenant_id=TEST_TENANT, session_id="ses_1",
        task_description="总结", status="RUNNING", execution_mode="inline",
        sandbox_id=None, started_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
        created_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
    )
    _install_fake_db_with_attachments(
        monkeypatch, run,
        [_FakeAttachment(filename="notes.txt", mime="text/plain", blob_key="k/notes")],
    )
    monkeypatch.setattr(ex, "_canvas_enabled_for_tenant", AsyncMock(return_value=False))
    monkeypatch.setattr(
        ex, "_inline_attachments_enabled_for_tenant", AsyncMock(return_value=True),
    )
    monkeypatch.setattr(ex.inline_skill_loader, "resolve_skills", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        ex, "_make_blob_store",
        lambda settings: _FakeBlobStore({
            "k/notes": "会话附件内容".encode(),
            "ws/handbook": "工作区手册内容".encode(),
        }),
    )

    lines = [
        _sse({"choices": [{"index": 0, "delta": {"content": "好的"}}]}),
        _usage_frame(3, 2, 5), "data: [DONE]",
    ]
    fake_client = _FakeGatewayClient(sse_lines=lines)
    _FakeRedisStream.reset()

    async def _run() -> None:
        with _patch_redis(_FakeRedisStream), \
                patch("httpx.AsyncClient", return_value=fake_client):
            await ex._execute_inline_run(
                settings=_attachment_settings(), tenant_id=TEST_TENANT, user_id=TEST_USER,
                session_id="ses_1", run_id="run_wsf", task="总结",
                model_hint="deepseek-chat", sys_prompt=None, memory="", history=[],
                sampling_params={}, skills=[],
                workspace_instructions="遵守团队规范。",
                workspace_files=[
                    {
                        "id": "wsf_handbook",
                        "filename": "handbook.txt",
                        "mime": "text/plain",
                        "key": "ws/handbook",
                    },
                ],
            )

    asyncio.get_event_loop().run_until_complete(_run())

    assert run.status == "COMPLETED"
    system_msg = fake_client.chat_payloads[0]["messages"][0]
    content = system_msg["content"]
    # The workspace-file id is surfaced as an id attribute on the tag.
    assert '<workspace_file id="wsf_handbook" filename="handbook.txt">' in content
    assert "工作区手册内容" in content
    assert '<attachment filename="notes.txt">' in content
    # Ordering: instructions → workspace files → session attachments → link convention.
    idx_instructions = content.index("# 工作区指令")
    idx_ws_file = content.index("<workspace_file")
    idx_attachment = content.index("<attachment")
    idx_convention = content.index("[文件名](#file:文件id)")
    assert idx_instructions < idx_ws_file < idx_attachment < idx_convention
    # The link convention appears exactly once even with two file blocks present.
    assert content.count("[文件名](#file:文件id)") == 1


def test_inline_no_workspace_params_behaviour_unchanged(monkeypatch) -> None:
    """Omitting the workspace params keeps the pre-feature behaviour: no
    workspace blocks, no system message when nothing else injects one."""
    from agent_session_orchestrator.services import inline_run_executor as ex

    run = Run(
        id="run_wsn", tenant_id=TEST_TENANT, session_id="ses_1",
        task_description="你好", status="RUNNING", execution_mode="inline",
        sandbox_id=None, started_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
        created_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
    )
    _install_fake_db_with_attachments(monkeypatch, run, [])
    monkeypatch.setattr(ex, "_canvas_enabled_for_tenant", AsyncMock(return_value=False))
    monkeypatch.setattr(
        ex, "_inline_attachments_enabled_for_tenant", AsyncMock(return_value=True),
    )
    monkeypatch.setattr(ex.inline_skill_loader, "resolve_skills", AsyncMock(return_value=[]))

    lines = [
        _sse({"choices": [{"index": 0, "delta": {"content": "你好"}}]}),
        _usage_frame(1, 1, 2), "data: [DONE]",
    ]
    fake_client = _FakeGatewayClient(sse_lines=lines)
    _FakeRedisStream.reset()

    async def _run() -> None:
        with _patch_redis(_FakeRedisStream), \
                patch("httpx.AsyncClient", return_value=fake_client):
            await ex._execute_inline_run(
                settings=_attachment_settings(), tenant_id=TEST_TENANT, user_id=TEST_USER,
                session_id="ses_1", run_id="run_wsn", task="你好",
                model_hint="deepseek-chat", sys_prompt=None, memory="", history=[],
                sampling_params={}, skills=[],
            )

    asyncio.get_event_loop().run_until_complete(_run())

    assert run.status == "COMPLETED"
    messages = fake_client.chat_payloads[0]["messages"]
    assert all(m["role"] != "system" for m in messages)
