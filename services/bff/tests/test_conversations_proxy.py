"""Tests for BFF conversations endpoints.

Covers:
1. POST /sessions/{id}/chat — unauthenticated → 401
2. POST /sessions/{id}/chat — happy path → JSON {run_id, message_id, run}
3. POST /sessions/{id}/chat — with attachment_ids → attachments endpoint called
4. POST /sessions/{id}/chat — unknown attachment id → skipped, run still returns
5. POST /sessions/{id}/chat — the message rides in the run request (one write)
6. POST /sessions/{id}/chat — run refused → upstream status + user-facing detail
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

    def __init__(self, lines: list[str], *, status_code: int = 200, body: bytes = b"",
                 fail_after: int | None = None) -> None:
        self._lines = lines
        self.status_code = status_code
        self._body = body
        self._fail_after = fail_after

    async def __aenter__(self) -> FakeStreamContext:
        return self

    async def __aexit__(self, *args: Any) -> None:
        pass

    async def aread(self) -> bytes:
        return self._body

    async def aiter_lines(self):  # type: ignore[override]
        for i, line in enumerate(self._lines):
            if self._fail_after is not None and i >= self._fail_after:
                raise httpx.ReadError("connection reset by peer at 10.0.0.7:8002")
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
        run_status: int = 200,
        run_error: dict[str, Any] | None = None,
        stream_lines: list[str] | None = None,
        attachment_items: list[dict] | None = None,
        stream_error: bool = False,
        message_sequence: int | None = None,
    ) -> None:
        self.run_status = run_status
        self.run_error = run_error
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
        self.message_sequence = message_sequence
        self.calls: list[tuple[str, str]] = []
        self.stream_params: list[dict[str, Any] | None] = []
        # Captures the JSON body of the POST /runs call so tests can assert what
        # the BFF forwarded to the orchestrator (e.g. execution_mode passthrough).
        self.runs_body: dict | None = None
        # Captures the headers of the POST /runs call so tests can assert
        # forwarding of X-User-Clearance (and other foundation_headers output)
        # onto the orchestrator-bound run-injection call.
        self.runs_headers: dict | None = None

    def route(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        self.calls.append((method, url))
        if "attachments" in url and method == "GET":
            items = self.attachment_items or []
            return httpx.Response(200, json={"items": items})
        if "runs" in url and method == "POST":
            body = kwargs.get("json") or {}
            self.runs_body = body
            self.runs_headers = dict(kwargs.get("headers") or {})
            if self.run_status >= 400:
                return httpx.Response(
                    self.run_status, json=self.run_error or {"detail": "upstream says no"},
                )
            run: dict[str, Any] = {
                "id": "run_01", "status": "QUEUED",
                "execution_mode": body.get("execution_mode", "sandbox"),
            }
            # The orchestrator writes the turn's message with the run and
            # returns it alongside.
            if body.get("message") is not None:
                run["message"] = {
                    "id": "msg_01", "role": "USER", "content": body["message"]["content"],
                }
                if self.message_sequence is not None:
                    run["message"]["sequence"] = self.message_sequence
            return httpx.Response(self.run_status, json=run)
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
            fake.stream_params.append(kw.get("params"))
            return fake.make_stream_ctx()

        async def aclose(self) -> None:
            pass

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
    assert "message" not in body["run"]

    # One write: the message rides in the run request, never on its own.
    methods_urls = [(m, u) for m, u in fake.calls]
    assert not any("messages" in u for _, u in methods_urls), "message posted on its own"
    assert any("runs" in u and m == "POST" for m, u in methods_urls), "runs POST missing"
    assert fake.runs_body is not None
    assert fake.runs_body["message"] == {"content": "hello", "attachment_ids": []}
    assert fake.runs_body["task"] == "hello"

    # No SSE stream should have been opened
    assert not any("stream" in m for m, _ in methods_urls), "stream was unexpectedly opened"


def test_first_message_schedules_automatic_title(
    client: TestClient,
    authed_cookie: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the first persisted message schedules the non-blocking title job."""
    from agent_bff.api.v1 import conversations

    fake = FakeChatFoundation(message_sequence=1)
    _patch_async_client(fake, monkeypatch)
    generate = AsyncMock()
    monkeypatch.setattr(conversations, "generate_and_update_session_title", generate)

    resp = client.post(
        "/api/bff/v1/sessions/ses_abc/chat",
        cookies=authed_cookie,
        json={"message": "设计航天反作用飞轮 FOC 控制验证", "model": "deepseek-fast"},
    )

    assert resp.status_code == 200
    generate.assert_awaited_once()
    kwargs = generate.await_args.kwargs
    assert kwargs["session_id"] == "ses_abc"
    assert kwargs["message"] == "设计航天反作用飞轮 FOC 控制验证"
    assert kwargs["model_hint"] == "deepseek-fast"


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
    assert fake.runs_body is not None
    assert fake.runs_body["message"]["attachment_ids"] == ["att_01"]
    assert [f["filename"] for f in fake.runs_body["files"]] == ["report.pdf"]


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
# Tests: POST /sessions/{id}/chat — execution_mode is no longer forwarded
# ---------------------------------------------------------------------------

def test_chat_does_not_forward_execution_mode(
    client: TestClient,
    authed_cookie: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every run is a sandboxed Claude SDK run: the BFF never forwards a mode,
    even when a legacy client still sends ``execution_mode``."""
    fake = FakeChatFoundation()
    _patch_async_client(fake, monkeypatch)

    resp = client.post(
        "/api/bff/v1/sessions/ses_abc/chat",
        cookies=authed_cookie,
        json={"message": "hello", "execution_mode": "inline"},
    )
    assert resp.status_code == 200
    assert fake.runs_body is not None
    assert "execution_mode" not in fake.runs_body


def test_chat_omits_skills_upstream_when_client_omits_them(
    client: TestClient,
    authed_cookie: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Absence must survive the BFF so orchestrator can inject defaults."""
    fake = FakeChatFoundation()
    _patch_async_client(fake, monkeypatch)

    resp = client.post(
        "/api/bff/v1/sessions/ses_abc/chat",
        cookies=authed_cookie,
        json={"message": "hello"},
    )

    assert resp.status_code == 200
    assert fake.runs_body is not None
    assert "skills" not in fake.runs_body


@pytest.mark.parametrize("skills", [[], ["pdf", "xlsx"]])
def test_chat_preserves_explicit_skill_selection(
    client: TestClient,
    authed_cookie: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    skills: list[str],
) -> None:
    """An explicit list, including [], overrides automatic defaults."""
    fake = FakeChatFoundation()
    _patch_async_client(fake, monkeypatch)

    resp = client.post(
        "/api/bff/v1/sessions/ses_abc/chat",
        cookies=authed_cookie,
        json={"message": "hello", "skills": skills},
    )

    assert resp.status_code == 200
    assert fake.runs_body is not None
    assert fake.runs_body["skills"] == skills


def test_chat_omits_mcp_server_ids_upstream_when_client_omits_them(
    client: TestClient,
    authed_cookie: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Absence must survive the BFF so the tenant's enabled set is used."""
    fake = FakeChatFoundation()
    _patch_async_client(fake, monkeypatch)

    resp = client.post(
        "/api/bff/v1/sessions/ses_abc/chat",
        cookies=authed_cookie,
        json={"message": "hello"},
    )

    assert resp.status_code == 200
    assert fake.runs_body is not None
    assert "mcp_server_ids" not in fake.runs_body


@pytest.mark.parametrize(
    "mcp_server_ids", [[], ["mcp_a"], ["mcp_a", "mcp_b"]]
)
def test_chat_preserves_explicit_mcp_server_selection(
    client: TestClient,
    authed_cookie: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    mcp_server_ids: list[str],
) -> None:
    """An explicit list, including [] (= disable all MCP), must be forwarded.

    ``[]`` is the regression this locks: a truthiness check in the BFF would
    drop the key and silently restore the auto-inject-everything behaviour.
    """
    fake = FakeChatFoundation()
    _patch_async_client(fake, monkeypatch)

    resp = client.post(
        "/api/bff/v1/sessions/ses_abc/chat",
        cookies=authed_cookie,
        json={"message": "hello", "mcp_server_ids": mcp_server_ids},
    )

    assert resp.status_code == 200
    assert fake.runs_body is not None
    assert fake.runs_body["mcp_server_ids"] == mcp_server_ids


# ---------------------------------------------------------------------------
# Tests: POST /sessions/{id}/chat — workspace_file_ids (@-referenced files)
# ---------------------------------------------------------------------------

def test_chat_forwards_workspace_file_ids(
    client: TestClient,
    authed_cookie: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """workspace_file_ids in the body is forwarded verbatim to the runs body."""
    fake = FakeChatFoundation()
    _patch_async_client(fake, monkeypatch)

    resp = client.post(
        "/api/bff/v1/sessions/ses_abc/chat",
        cookies=authed_cookie,
        json={"message": "看看这个", "workspace_file_ids": ["wsf_1", "wsf_2"]},
    )
    assert resp.status_code == 200
    assert fake.runs_body is not None
    assert fake.runs_body.get("workspace_file_ids") == ["wsf_1", "wsf_2"]


def test_chat_omits_workspace_file_ids_when_absent(
    client: TestClient,
    authed_cookie: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No workspace_file_ids in the body → the key is not added to the runs body."""
    fake = FakeChatFoundation()
    _patch_async_client(fake, monkeypatch)

    resp = client.post(
        "/api/bff/v1/sessions/ses_abc/chat",
        cookies=authed_cookie,
        json={"message": "hello"},
    )
    assert resp.status_code == 200
    assert fake.runs_body is not None
    assert "workspace_file_ids" not in fake.runs_body


def test_chat_workspace_file_ids_invalid_type_422(
    client: TestClient,
    authed_cookie: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An illegal workspace_file_ids type (non-array) → 422, no runs POST."""
    fake = FakeChatFoundation()
    _patch_async_client(fake, monkeypatch)

    resp = client.post(
        "/api/bff/v1/sessions/ses_abc/chat",
        cookies=authed_cookie,
        json={"message": "hello", "workspace_file_ids": "wsf_1"},
    )
    assert resp.status_code == 422
    # Validation happens before any orchestrator call → no runs POST attempted.
    assert not any("runs" in u and m == "POST" for m, u in fake.calls)


# ---------------------------------------------------------------------------
# Tests: POST /sessions/{id}/chat - clearance forwarding (X-User-Clearance)
# ---------------------------------------------------------------------------

def test_chat_forwards_clearance_on_runs_call(
    client: TestClient,
    authed_cookie_with_clearance: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The caller's clearance is forwarded as X-User-Clearance on the /runs POST.

    Regression guard: the ``chat`` signature must carry ``clearance`` through
    to ``foundation_headers`` so the orchestrator's run-injection
    (load_workspace_context) honours the caller's level instead of falling
    back to L2 and skipping L3+ workspaces entirely.
    """
    fake = FakeChatFoundation()
    _patch_async_client(fake, monkeypatch)

    resp = client.post(
        "/api/bff/v1/sessions/ses_abc/chat",
        cookies=authed_cookie_with_clearance,
        json={"message": "hello"},
    )
    assert resp.status_code == 200
    assert fake.runs_headers is not None, "expected a POST /runs call"
    assert fake.runs_headers.get("X-User-Clearance") == "L3", (
        f"expected X-User-Clearance: L3 forwarded on /runs, got {fake.runs_headers}"
    )


def test_chat_omits_clearance_when_cookie_lacks_claim(
    client: TestClient,
    authed_cookie: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No clearance claim in the cookie -> no X-User-Clearance header.

    Service-token parity: the base ``authed_cookie`` bakes no ``clearance``
    field, so the ``current_clearance`` contextvar resolves to "" and
    ``foundation_headers`` omits the header entirely.  Guards against the BFF
    inventing a default level on the caller's behalf.
    """
    fake = FakeChatFoundation()
    _patch_async_client(fake, monkeypatch)

    resp = client.post(
        "/api/bff/v1/sessions/ses_abc/chat",
        cookies=authed_cookie,
        json={"message": "hello"},
    )
    assert resp.status_code == 200
    assert fake.runs_headers is not None, "expected a POST /runs call"
    assert "X-User-Clearance" not in fake.runs_headers, (
        f"X-User-Clearance must not be sent without a claim, got {fake.runs_headers}"
    )


# ---------------------------------------------------------------------------
# Tests: POST /sessions/{id}/chat — upstream error propagation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("upstream", "expected"),
    [
        (409, "already in progress"),
        (404, "no longer exists"),
        (403, "permission"),
        (500, "could not be started"),
        (502, "could not be started"),
        (503, "could not be started"),
    ],
)
def test_chat_refused_run_relays_status_with_user_facing_detail(
    client: TestClient,
    authed_cookie: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    upstream: int,
    expected: str,
) -> None:
    """A refused run keeps the upstream status; the detail is written for the user."""
    from agent_bff.api.v1 import conversations

    fake = FakeChatFoundation(
        run_status=upstream,
        run_error={"detail": "scheduler rejected run (HTTP 500)", "code": "AGENT.X"},
        message_sequence=1,
    )
    _patch_async_client(fake, monkeypatch)
    generate = AsyncMock()
    monkeypatch.setattr(conversations, "generate_and_update_session_title", generate)

    resp = client.post(
        "/api/bff/v1/sessions/ses_abc/chat",
        cookies=authed_cookie,
        json={"message": "hello"},
    )
    assert resp.status_code == upstream
    body = resp.json()
    assert expected in body["detail"]
    assert "scheduler" not in body["detail"]
    assert body["code"] == "AGENT.X"
    # Nothing was saved, so nothing names the session.
    generate.assert_not_awaited()


@pytest.mark.parametrize(
    ("upstream", "code", "user_message"),
    [
        (422, "AGENT.RUN.LLM_NOT_CONFIGURED",
         "No AI model is set up for your organization yet, so the agent can't run."),
        (402, "AGENT.RUN.QUOTA_EXCEEDED",
         "Your organization has reached its limit for agent runs."),
    ],
)
def test_chat_refusal_the_user_can_act_on_relays_its_message(
    client: TestClient,
    authed_cookie: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    upstream: int,
    code: str,
    user_message: str,
) -> None:
    """Retrying won't help with these, so the user is told what will instead of
    "try again in a moment"."""
    fake = FakeChatFoundation(
        run_status=upstream,
        run_error={"detail": "operator wording", "code": code, "user_message": user_message},
        message_sequence=1,
    )
    _patch_async_client(fake, monkeypatch)

    resp = client.post(
        "/api/bff/v1/sessions/ses_abc/chat",
        cookies=authed_cookie,
        json={"message": "hello"},
    )
    assert resp.status_code == upstream
    assert resp.json() == {"detail": user_message, "status": upstream, "code": code}


def test_chat_refusal_does_not_relay_other_orchestrator_messages(
    client: TestClient,
    authed_cookie: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``user_message`` defaults to the operator-facing detail on most
    orchestrator errors, so only the listed refusals are trusted with it."""
    fake = FakeChatFoundation(
        run_status=404,
        run_error={"code": "AGENT.SESSION.NOT_FOUND", "detail": "Session ses_abc not found",
                   "user_message": "Session ses_abc not found"},
        message_sequence=1,
    )
    _patch_async_client(fake, monkeypatch)

    resp = client.post(
        "/api/bff/v1/sessions/ses_abc/chat",
        cookies=authed_cookie,
        json={"message": "hello"},
    )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "This conversation no longer exists."


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


def test_stream_forwards_the_after_cursor(
    client: TestClient,
    authed_cookie: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reconnect's cursor reaches the orchestrator; a first connect sends none."""
    fake = FakeChatFoundation()
    _patch_async_client(fake, monkeypatch)

    client.get(
        "/api/bff/v1/sessions/ses_abc/stream?after=42&after_id=msg_42", cookies=authed_cookie,
    )
    client.get("/api/bff/v1/sessions/ses_abc/stream", cookies=authed_cookie)

    assert fake.stream_params == [{"after": 42, "after_id": "msg_42"}, None]


def test_stream_rejects_a_negative_after_cursor(
    client: TestClient,
    authed_cookie: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeChatFoundation()
    _patch_async_client(fake, monkeypatch)

    resp = client.get("/api/bff/v1/sessions/ses_abc/stream?after=-1", cookies=authed_cookie)

    assert resp.status_code == 422
    assert fake.stream_params == []


def test_stream_upstream_connection_error_is_502(
    client: TestClient,
    authed_cookie: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unreachable orchestrator is an HTTP 502, not an empty 200 stream."""
    fake = FakeChatFoundation(stream_error=True)
    _patch_async_client(fake, monkeypatch)

    resp = client.get(
        "/api/bff/v1/sessions/ses_abc/stream",
        cookies=authed_cookie,
    )
    assert resp.status_code == 502
    assert resp.json()["detail"] == "Stream unavailable"
    assert "upstream unavailable" not in resp.text


@pytest.mark.parametrize("upstream_status", [403, 404])
def test_stream_passes_upstream_4xx_through(
    client: TestClient,
    authed_cookie: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    upstream_status: int,
) -> None:
    """A deleted / foreign session keeps its 4xx so the client stops reconnecting."""
    fake = FakeChatFoundation()
    fake.make_stream_ctx = lambda: FakeStreamContext(  # type: ignore[method-assign]
        [], status_code=upstream_status, body=b'{"detail": "Session not found"}',
    )
    _patch_async_client(fake, monkeypatch)

    resp = client.get("/api/bff/v1/sessions/ses_abc/stream", cookies=authed_cookie)
    assert resp.status_code == upstream_status
    assert resp.json()["detail"] == "Session not found"


def test_stream_lost_midway_emits_generic_error_frame(
    client: TestClient,
    authed_cookie: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A connection lost mid-stream ends with an error frame free of internals."""
    fake = FakeChatFoundation()
    fake.make_stream_ctx = lambda: FakeStreamContext(  # type: ignore[method-assign]
        ["event: ping", "data: {}", "", "event: token"], fail_after=3,
    )
    _patch_async_client(fake, monkeypatch)

    resp = client.get("/api/bff/v1/sessions/ses_abc/stream", cookies=authed_cookie)
    assert resp.status_code == 200
    assert "event: ping" in resp.text
    assert "event: error" in resp.text
    assert "Stream connection lost" in resp.text
    assert "10.0.0.7" not in resp.text


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
