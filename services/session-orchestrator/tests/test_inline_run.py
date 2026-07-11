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

    usage, body = asyncio.get_event_loop().run_until_complete(_run())

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
