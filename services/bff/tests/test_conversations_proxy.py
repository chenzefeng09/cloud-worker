"""Tests for BFF conversations endpoint (POST /sessions/{id}/chat SSE proxy).

Covers:
1. Unauthenticated → 401
2. Authenticated, happy path → StreamingResponse with SSE events
3. Authenticated, message creation fails upstream → SSE error event
4. Authenticated, run creation fails upstream → SSE error event
5. _resolve_attachment_files helpers: empty ids, success, fetch failure, missing id
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Helper: fake httpx.AsyncClient that handles the streaming chat workflow
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


class FakeChatFoundation:
    """Stub that handles the multi-step chat workflow."""

    def __init__(
        self,
        *,
        message_status: int = 200,
        run_status: int = 200,
        stream_lines: list[str] | None = None,
        attachment_items: list[dict] | None = None,
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

    def make_stream_ctx(self) -> FakeStreamContext:
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

        def stream(self, method: str, url: str, **kw: Any) -> FakeStreamContext:
            fake.calls.append((method + "_stream", url))
            return fake.make_stream_ctx()

    monkeypatch.setattr(httpx, "AsyncClient", _AC)


# ---------------------------------------------------------------------------
# Tests: authentication guard
# ---------------------------------------------------------------------------

def test_chat_requires_auth(client: TestClient) -> None:
    """No session cookie → 401."""
    resp = client.post(
        "/api/bff/v1/sessions/ses_abc/chat",
        json={"message": "hello"},
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Tests: happy path
# ---------------------------------------------------------------------------

def test_chat_happy_path_returns_sse_events(
    client: TestClient,
    authed_cookie: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Authenticated chat returns a StreamingResponse with SSE events."""
    fake = FakeChatFoundation()
    _patch_async_client(fake, monkeypatch)

    resp = client.post(
        "/api/bff/v1/sessions/ses_abc/chat",
        cookies=authed_cookie,
        json={"message": "hello"},
    )
    # StreamingResponse returns 200; test client collects the full body
    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers.get("content-type", "")

    # Should have emitted message_created and run_started events
    content = resp.text
    assert "message_created" in content
    assert "run_started" in content


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
    # Verify attachments endpoint was called
    att_calls = [c for c in fake.calls if "attachments" in c[1]]
    assert att_calls, "Expected attachment list call"


def test_chat_attachment_with_unknown_id(
    client: TestClient,
    authed_cookie: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If attachment id not found in response, it is skipped (failure-open)."""
    # attachments endpoint returns empty items — unknown id is logged, not fatal
    fake = FakeChatFoundation(attachment_items=[])
    _patch_async_client(fake, monkeypatch)

    resp = client.post(
        "/api/bff/v1/sessions/ses_abc/chat",
        cookies=authed_cookie,
        json={"message": "summarize", "attachment_ids": ["att_missing"]},
    )
    assert resp.status_code == 200
    content = resp.text
    # Run should still start — failure-open
    assert "run_started" in content


# ---------------------------------------------------------------------------
# Tests: upstream error propagation via SSE error events
# ---------------------------------------------------------------------------

def test_chat_message_creation_fails_emits_sse_error(
    client: TestClient,
    authed_cookie: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If orchestrator returns 5xx on message creation, SSE error event is emitted."""
    fake = FakeChatFoundation(message_status=500)
    _patch_async_client(fake, monkeypatch)

    resp = client.post(
        "/api/bff/v1/sessions/ses_abc/chat",
        cookies=authed_cookie,
        json={"message": "hello"},
    )
    assert resp.status_code == 200
    content = resp.text
    assert "event: error" in content
    # Error data should mention the failure
    assert "Failed to save message" in content


def test_chat_run_creation_fails_emits_sse_error(
    client: TestClient,
    authed_cookie: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If orchestrator returns 5xx on run creation, SSE error event is emitted."""
    fake = FakeChatFoundation(run_status=500)
    _patch_async_client(fake, monkeypatch)

    resp = client.post(
        "/api/bff/v1/sessions/ses_abc/chat",
        cookies=authed_cookie,
        json={"message": "hello"},
    )
    assert resp.status_code == 200
    content = resp.text
    assert "event: error" in content
    assert "Failed to start run" in content


# ---------------------------------------------------------------------------
# Tests: _resolve_attachment_files standalone (unit-level)
# ---------------------------------------------------------------------------

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
