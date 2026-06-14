"""Tests for BFF conversations endpoints.

Covers:
1. POST /sessions/{id}/chat — unauthenticated → 401
2. POST /sessions/{id}/chat — happy path → JSON {run_id, message_id, run}
3. POST /sessions/{id}/chat — with attachment_ids → attachments endpoint called
4. POST /sessions/{id}/chat — unknown attachment id → skipped, run still returns
5. POST /sessions/{id}/chat — message creation fails → JSON error (upstream status)
6. POST /sessions/{id}/chat — run creation fails → JSON error (upstream status)
7. GET  /sessions/{id}/stream — unauthenticated → 401
8. GET  /sessions/{id}/stream — happy path → SSE relay
9. GET  /sessions/{id}/stream — upstream connection error → SSE error frame
10. _resolve_attachment_files helpers: empty ids, success, fetch failure, missing id
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Helper: fake httpx.AsyncClient that handles both regular + streaming calls
# ---------------------------------------------------------------------------

class FakeStreamContext:
    """Mimics httpx.AsyncClient.stream() context manager for SSE relay tests."""

    def __init__(self, lines: list[str]) -> None:
        self._lines = lines

    async def __aenter__(self) -> FakeStreamContext:
        return self

    async def __aexit__(self, *args: Any) -> None:
        pass

    async def aiter_lines(self):  # type: ignore[override]
        for line in self._lines:
            yield line


class FakeStreamContextError:
    """Stream context that raises on __aenter__ to simulate connection failure."""

    async def __aenter__(self) -> FakeStreamContextError:
        raise httpx.ConnectError("upstream unavailable")

    async def __aexit__(self, *args: Any) -> None:
        pass


class FakeChatFoundation:
    """Stub that handles the multi-step chat + stream workflow."""

    def __init__(
        self,
        *,
        message_status: int = 200,
        run_status: int = 200,
        stream_lines: list[str] | None = None,
        attachment_items: list[dict] | None = None,
        stream_error: bool = False,
    ) -> None:
        self.message_status = message_status
        self.run_status = run_status
        self.stream_lines: list[str] = stream_lines if stream_lines is not None else [
            "event: token",
            "data: hello",
            "",
            "event: done",
            "data: {}",
            "",
        ]
        self.attachment_items = attachment_items  # None means no attachment endpoint
        self.stream_error = stream_error
        self.calls: list[tuple[str, str]] = []

    def route(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        self.calls.append((method, url))
        if "messages" in url and method == "POST":
            return httpx.Response(
                self.message_status,
                json={"id": "msg_01", "role": "USER", "content": "hi"},
            )
        if "attachments" in url and method == "GET":
            items = self.attachment_items or []
            return httpx.Response(200, json={"items": items})
        if "runs" in url and method == "POST":
            return httpx.Response(
                self.run_status,
                json={"id": "run_01", "status": "QUEUED"},
            )
        return httpx.Response(200, json={"ok": True})

    def make_stream_ctx(self) -> FakeStreamContext | FakeStreamContextError:
        if self.stream_error:
            return FakeStreamContextError()
        return FakeStreamContext(self.stream_lines)


def _patch_async_client(fake: FakeChatFoundation, monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace httpx.AsyncClient with a fake that supports both regular calls and stream()."""

    class _AC:
        def __init__(self, *a: Any, **kw: Any) -> None:
            pass

        async def __aenter__(self) -> _AC:
            return self

        async def __aexit__(self, *a: Any) -> None:
            pass

        async def get(self, url: str, **kw: Any) -> httpx.Response:
            return fake.route("GET", url, **kw)

        async def post(self, url: str, **kw: Any) -> httpx.Response:
            return fake.route("POST", url, **kw)

        def stream(self, method: str, url: str, **kw: Any):
            fake.calls.append((method + "_stream", url))
            return fake.make_stream_ctx()

    monkeypatch.setattr(httpx, "AsyncClient", _AC)


# ---------------------------------------------------------------------------
# Tests: POST /sessions/{id}/chat — authentication guard
# ---------------------------------------------------------------------------

def test_chat_requires_auth(client: TestClient) -> None:
    """No session cookie → 401."""
    resp = client.post(
        "/api/bff/v1/sessions/ses_abc/chat",
        json={"message": "hello"},
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Tests: POST /sessions/{id}/chat — happy path
# ---------------------------------------------------------------------------

def test_chat_happy_path_returns_json(
    client: TestClient,
    authed_cookie: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Authenticated chat returns JSON {run_id, message_id, run} — NOT SSE."""
    fake = FakeChatFoundation()
    _patch_async_client(fake, monkeypatch)

    resp = client.post(
        "/api/bff/v1/sessions/ses_abc/chat",
        cookies=authed_cookie,
        json={"message": "hello"},
    )
    assert resp.status_code == 200
    # Must be JSON, not SSE
    assert "application/json" in resp.headers.get("content-type", "")

    body = resp.json()
    assert body["run_id"] == "run_01"
    assert body["message_id"] == "msg_01"
    assert body["run"]["id"] == "run_01"

    # Both upstream POSTs must have happened
    methods_urls = [(m, u) for m, u in fake.calls]
    assert any("messages" in u and m == "POST" for m, u in methods_urls), "messages POST missing"
    assert any("runs" in u and m == "POST" for m, u in methods_urls), "runs POST missing"

    # No SSE stream should have been opened
    assert not any("stream" in m for m, _ in methods_urls), "stream was unexpectedly opened"


def test_chat_with_attachment_ids(
    client: TestClient,
    authed_cookie: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Chat with attachment_ids fetches attachments and includes them in run."""
    attachment_items = [
        {
            "id": "att_01",
            "filename": "report.pdf",
            "blob_key": "tenant/t/sessions/ses_abc/attachments/att_01/report.pdf",
            "mime": "application/pdf",
        }
    ]
    fake = FakeChatFoundation(attachment_items=attachment_items)
    _patch_async_client(fake, monkeypatch)

    resp = client.post(
        "/api/bff/v1/sessions/ses_abc/chat",
        cookies=authed_cookie,
        json={"message": "summarize", "attachment_ids": ["att_01"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["run_id"] == "run_01"

    # Verify attachments endpoint was called
    att_calls = [c for c in fake.calls if "attachments" in c[1]]
    assert att_calls, "Expected attachment list call"


def test_chat_attachment_with_unknown_id(
    client: TestClient,
    authed_cookie: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If attachment id not found in response, it is skipped (failure-open)."""
    fake = FakeChatFoundation(attachment_items=[])
    _patch_async_client(fake, monkeypatch)

    resp = client.post(
        "/api/bff/v1/sessions/ses_abc/chat",
        cookies=authed_cookie,
        json={"message": "summarize", "attachment_ids": ["att_missing"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    # Run should still have started
    assert body["run_id"] == "run_01"


# ---------------------------------------------------------------------------
# Tests: POST /sessions/{id}/chat — upstream error propagation
# ---------------------------------------------------------------------------

def test_chat_message_creation_fails_returns_error(
    client: TestClient,
    authed_cookie: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If orchestrator returns 5xx on message creation, JSON error with upstream status."""
    fake = FakeChatFoundation(message_status=500)
    _patch_async_client(fake, monkeypatch)

    resp = client.post(
        "/api/bff/v1/sessions/ses_abc/chat",
        cookies=authed_cookie,
        json={"message": "hello"},
    )
    assert resp.status_code == 500
    body = resp.json()
    assert "Failed to save message" in body.get("detail", "")
    # No runs POST should have been attempted
    assert not any("runs" in u and m == "POST" for m, u in fake.calls)


def test_chat_run_creation_fails_returns_error(
    client: TestClient,
    authed_cookie: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If orchestrator returns 5xx on run creation, JSON error with upstream status."""
    fake = FakeChatFoundation(run_status=500)
    _patch_async_client(fake, monkeypatch)

    resp = client.post(
        "/api/bff/v1/sessions/ses_abc/chat",
        cookies=authed_cookie,
        json={"message": "hello"},
    )
    assert resp.status_code == 500
    body = resp.json()
    assert "Failed to start run" in body.get("detail", "")


def test_chat_message_creation_fails_404_returns_404(
    client: TestClient,
    authed_cookie: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Upstream 404 propagates the 404 status code."""
    fake = FakeChatFoundation(message_status=404)
    _patch_async_client(fake, monkeypatch)

    resp = client.post(
        "/api/bff/v1/sessions/ses_abc/chat",
        cookies=authed_cookie,
        json={"message": "hello"},
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Tests: GET /sessions/{id}/stream — authentication guard
# ---------------------------------------------------------------------------

def test_stream_requires_auth(client: TestClient) -> None:
    """No session cookie → 401."""
    resp = client.get("/api/bff/v1/sessions/ses_abc/stream")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Tests: GET /sessions/{id}/stream — happy path SSE relay
# ---------------------------------------------------------------------------

def test_stream_relays_sse_events(
    client: TestClient,
    authed_cookie: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Authenticated stream GET relays SSE lines verbatim from orchestrator."""
    sse_lines = [
        "event: run_started",
        "data: {}",
        "",
        "event: token",
        "data: hello",
        "",
        "event: run_complete",
        "data: {}",
        "",
        "event: done",
        "data: {}",
        "",
    ]
    fake = FakeChatFoundation(stream_lines=sse_lines)
    _patch_async_client(fake, monkeypatch)

    resp = client.get(
        "/api/bff/v1/sessions/ses_abc/stream",
        cookies=authed_cookie,
    )
    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers.get("content-type", "")

    content = resp.text
    assert "event: run_started" in content
    assert "event: token" in content
    assert "data: hello" in content
    assert "event: done" in content

    # The NEW orchestrator endpoint must have been used (/stream, not /messages/stream)
    stream_calls = [(m, u) for m, u in fake.calls if "stream" in m.lower() or "stream" in u]
    assert stream_calls, "Expected a stream call"
    # Verify it targets the session-scoped /stream endpoint
    assert any("/stream" in u for _, u in stream_calls)
    assert not any("/messages/stream" in u for _, u in stream_calls), (
        "Old /messages/stream endpoint was called — should be /sessions/{id}/stream"
    )


def test_stream_upstream_connection_error_emits_error_frame(
    client: TestClient,
    authed_cookie: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If upstream stream connection fails, an SSE error frame is emitted."""
    fake = FakeChatFoundation(stream_error=True)
    _patch_async_client(fake, monkeypatch)

    resp = client.get(
        "/api/bff/v1/sessions/ses_abc/stream",
        cookies=authed_cookie,
    )
    assert resp.status_code == 200  # StreamingResponse still 200; error is in body
    content = resp.text
    assert "event: error" in content
    assert "Stream connection failed" in content


# ---------------------------------------------------------------------------
# Tests: _resolve_attachment_files standalone (unit-level)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Tests: POST /sessions/{id}/runs/{run_id}/respond — HITL proxy
# ---------------------------------------------------------------------------

class FakeRespondFoundation:
    """Stub for the HITL respond endpoint tests."""

    def __init__(self, *, respond_status: int = 200) -> None:
        self.respond_status = respond_status
        self.calls: list[tuple[str, str]] = []

    def route(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        self.calls.append((method, url))
        if "respond" in url and method == "POST":
            if self.respond_status == 200:
                return httpx.Response(200, json={"run_id": "run_01", "status": "RUNNING"})
            return httpx.Response(self.respond_status, json={"detail": "error"})
        return httpx.Response(200, json={"ok": True})


def _patch_async_client_respond(fake: FakeRespondFoundation, monkeypatch: pytest.MonkeyPatch) -> None:
    class _AC:
        def __init__(self, *a: Any, **kw: Any) -> None:
            pass

        async def __aenter__(self) -> _AC:
            return self

        async def __aexit__(self, *a: Any) -> None:
            pass

        async def post(self, url: str, **kw: Any) -> httpx.Response:
            return fake.route("POST", url, **kw)

        def stream(self, method: str, url: str, **kw: Any):
            return FakeStreamContextError()

    monkeypatch.setattr(httpx, "AsyncClient", _AC)


def test_respond_requires_auth(client: TestClient) -> None:
    """No session cookie → 401."""
    resp = client.post(
        "/api/bff/v1/sessions/ses_abc/runs/run_01/respond",
        json={"kind": "confirm", "tool_call_id": "tc_01", "confirmed": True},
    )
    assert resp.status_code == 401


def test_respond_happy_confirm(
    client: TestClient,
    authed_cookie: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Authenticated confirm → orchestrator called, 200 relayed."""
    fake = FakeRespondFoundation()
    _patch_async_client_respond(fake, monkeypatch)

    resp = client.post(
        "/api/bff/v1/sessions/ses_abc/runs/run_01/respond",
        cookies=authed_cookie,
        json={"kind": "confirm", "tool_call_id": "tc_01", "confirmed": True},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["run_id"] == "run_01"
    assert body["status"] == "RUNNING"
    assert any("respond" in u for _, u in fake.calls)


def test_respond_happy_answer(
    client: TestClient,
    authed_cookie: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Authenticated answer → orchestrator called, 200 relayed."""
    fake = FakeRespondFoundation()
    _patch_async_client_respond(fake, monkeypatch)

    resp = client.post(
        "/api/bff/v1/sessions/ses_abc/runs/run_01/respond",
        cookies=authed_cookie,
        json={"kind": "answer", "tool_call_id": "tc_02", "answer": "yes"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["run_id"] == "run_01"


def test_respond_upstream_409_relayed(
    client: TestClient,
    authed_cookie: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Upstream 409 (run not paused) → 409 relayed to the caller."""
    fake = FakeRespondFoundation(respond_status=409)
    _patch_async_client_respond(fake, monkeypatch)

    resp = client.post(
        "/api/bff/v1/sessions/ses_abc/runs/run_01/respond",
        cookies=authed_cookie,
        json={"kind": "confirm", "tool_call_id": "tc_01", "confirmed": False},
    )
    assert resp.status_code == 409


def test_respond_invalid_kind_returns_400(
    client: TestClient,
    authed_cookie: dict[str, str],
) -> None:
    """Invalid kind → 400 without calling the orchestrator."""
    resp = client.post(
        "/api/bff/v1/sessions/ses_abc/runs/run_01/respond",
        cookies=authed_cookie,
        json={"kind": "invalid", "tool_call_id": "tc_01"},
    )
    assert resp.status_code == 400
    assert "kind" in resp.json().get("detail", "")


def test_respond_missing_tool_call_id_returns_400(
    client: TestClient,
    authed_cookie: dict[str, str],
) -> None:
    """Missing tool_call_id → 400 without calling the orchestrator."""
    resp = client.post(
        "/api/bff/v1/sessions/ses_abc/runs/run_01/respond",
        cookies=authed_cookie,
        json={"kind": "confirm", "tool_call_id": ""},
    )
    assert resp.status_code == 400
    assert "tool_call_id" in resp.json().get("detail", "")


# ---------------------------------------------------------------------------
# Tests: GET /sessions/{id}/pending — HITL rehydration proxy
# ---------------------------------------------------------------------------

class FakePendingFoundation:
    """Stub for the GET /pending proxy tests."""

    def __init__(self, *, pending_status: int = 200, items: list[dict] | None = None) -> None:
        self.pending_status = pending_status
        self.items = items if items is not None else []
        self.calls: list[tuple[str, str]] = []

    def route(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        self.calls.append((method, url))
        if "pending" in url and method == "GET":
            return httpx.Response(self.pending_status, json={"items": self.items})
        return httpx.Response(200, json={"ok": True})


def _patch_async_client_pending(fake: FakePendingFoundation, monkeypatch: pytest.MonkeyPatch) -> None:
    class _AC:
        def __init__(self, *a: Any, **kw: Any) -> None:
            pass

        async def __aenter__(self) -> _AC:
            return self

        async def __aexit__(self, *a: Any) -> None:
            pass

        async def get(self, url: str, **kw: Any) -> httpx.Response:
            return fake.route("GET", url, **kw)

    monkeypatch.setattr(httpx, "AsyncClient", _AC)


def test_pending_requires_auth(client: TestClient) -> None:
    """No session cookie → 401."""
    resp = client.get("/api/bff/v1/sessions/ses_abc/pending")
    assert resp.status_code == 401


def test_pending_relays_items(
    client: TestClient,
    authed_cookie: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Authenticated GET → orchestrator /pending called, items relayed verbatim."""
    items = [
        {
            "run_id": "run_01",
            "kind": "confirm",
            "tool_calls": [{"id": "tc_01", "name": "Bash", "args": "{}"}],
            "question": None,
            "paused_at": "2026-01-01T00:00:00+00:00",
        }
    ]
    fake = FakePendingFoundation(items=items)
    _patch_async_client_pending(fake, monkeypatch)

    resp = client.get(
        "/api/bff/v1/sessions/ses_abc/pending",
        cookies=authed_cookie,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["items"] == items
    # The orchestrator /pending endpoint must have been hit.
    assert any("pending" in u and m == "GET" for m, u in fake.calls)


def test_pending_relays_empty(
    client: TestClient,
    authed_cookie: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty items relayed as-is (no pause active)."""
    fake = FakePendingFoundation(items=[])
    _patch_async_client_pending(fake, monkeypatch)

    resp = client.get(
        "/api/bff/v1/sessions/ses_abc/pending",
        cookies=authed_cookie,
    )
    assert resp.status_code == 200
    assert resp.json() == {"items": []}


@pytest.mark.asyncio
async def test_resolve_attachment_files_empty_ids() -> None:
    """Empty attachment_ids returns immediately without HTTP calls."""
    from agent_bff.api.v1.conversations import _resolve_attachment_files

    client_mock = MagicMock()
    result = await _resolve_attachment_files(
        client=client_mock,
        orch_url="http://orch",
        session_id="ses_01",
        attachment_ids=[],
        headers={},
    )
    assert result == []
    client_mock.get.assert_not_called()


@pytest.mark.asyncio
async def test_resolve_attachment_files_upstream_error_returns_empty() -> None:
    """If attachment fetch throws, failure-open returns []."""
    from agent_bff.api.v1.conversations import _resolve_attachment_files

    client_mock = AsyncMock()
    client_mock.get.side_effect = httpx.ConnectError("timeout")

    result = await _resolve_attachment_files(
        client=client_mock,
        orch_url="http://orch",
        session_id="ses_01",
        attachment_ids=["att_01"],
        headers={},
    )
    assert result == []


@pytest.mark.asyncio
async def test_resolve_attachment_files_non_200_returns_empty() -> None:
    """If attachment fetch returns non-200, failure-open returns []."""
    from agent_bff.api.v1.conversations import _resolve_attachment_files

    client_mock = AsyncMock()
    client_mock.get.return_value = httpx.Response(503, json={})

    result = await _resolve_attachment_files(
        client=client_mock,
        orch_url="http://orch",
        session_id="ses_01",
        attachment_ids=["att_01"],
        headers={},
    )
    assert result == []


@pytest.mark.asyncio
async def test_resolve_attachment_files_success() -> None:
    """Happy path returns correct files list."""
    from agent_bff.api.v1.conversations import _resolve_attachment_files

    client_mock = AsyncMock()
    client_mock.get.return_value = httpx.Response(
        200,
        json={
            "items": [
                {
                    "id": "att_01",
                    "filename": "doc.pdf",
                    "blob_key": "tenant/t/s/att_01/doc.pdf",
                    "mime": "application/pdf",
                }
            ]
        },
    )

    result = await _resolve_attachment_files(
        client=client_mock,
        orch_url="http://orch",
        session_id="ses_01",
        attachment_ids=["att_01"],
        headers={},
    )
    assert len(result) == 1
    assert result[0]["filename"] == "doc.pdf"
    assert result[0]["key"] == "tenant/t/s/att_01/doc.pdf"
    assert result[0]["mime"] == "application/pdf"
