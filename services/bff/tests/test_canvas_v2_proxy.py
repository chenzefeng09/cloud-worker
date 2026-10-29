"""Tests for the BFF canvas v2 CRUD/version/suggestion proxy routes.

Covers:
1. proxy routes require auth (401 without cookie).
2. proxy routes relay status + JSON body verbatim, with foundation headers
   forwarded to the orchestrator (X-Tenant-Id present).
3. status passthrough for non-2xx upstream responses (e.g. 404).
4. the ``base_version`` optimistic-lock pre-check on the edit endpoint:
   stale base_version short-circuits with 409 before the LLM is ever
   called; matching/omitted base_version proceeds normally.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from agent_bff.api.v1 import canvas as canvas_mod

PREFIX = "/api/bff/v1"


@pytest.fixture(autouse=True)
def _clear_vkey_cache() -> None:
    canvas_mod._vkey_cache.clear()


# ---------------------------------------------------------------------------
# Fake orchestrator/gateway for the dumb proxy routes
# ---------------------------------------------------------------------------

class FakeOrchestrator:
    """Stub httpx target for the canvas_v2_proxy routes."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict | None, dict | None]] = []
        self.responses: dict[tuple[str, str], httpx.Response] = {}

    def set_response(self, method: str, path_fragment: str, resp: httpx.Response) -> None:
        self.responses[(method, path_fragment)] = resp

    def route(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        self.calls.append((method, url, kwargs.get("headers"), kwargs.get("json")))
        for (m, frag), resp in self.responses.items():
            if m == method and frag in url:
                return resp
        return httpx.Response(200, json={"ok": True})


def _patch_orch(fake: FakeOrchestrator, monkeypatch: pytest.MonkeyPatch) -> None:
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

    monkeypatch.setattr(httpx, "AsyncClient", _AC)


# ---------------------------------------------------------------------------
# Auth guard
# ---------------------------------------------------------------------------

def test_create_canvas_proxy_requires_auth(client: TestClient) -> None:
    resp = client.post(
        f"{PREFIX}/sessions/ses_abc/canvases", json={"title": "文档"}
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Happy paths — status + body relay, headers forwarded
# ---------------------------------------------------------------------------

def test_create_canvas_proxy_happy(
    client: TestClient, authed_cookie: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeOrchestrator()
    fake.set_response(
        "POST",
        "/sessions/ses_abc/canvases",
        httpx.Response(201, json={"id": "cvs_1", "current_version": 1, "blocks": []}),
    )
    _patch_orch(fake, monkeypatch)

    resp = client.post(
        f"{PREFIX}/sessions/ses_abc/canvases",
        cookies=authed_cookie,
        json={"title": "新文档"},
    )
    assert resp.status_code == 201
    assert resp.json() == {"id": "cvs_1", "current_version": 1, "blocks": []}

    create_calls = [
        c for c in fake.calls if c[0] == "POST" and "/sessions/ses_abc/canvases" in c[1]
    ]
    assert len(create_calls) == 1
    _, url, headers, body = create_calls[0]
    assert url.endswith("/sessions/ses_abc/canvases")
    assert headers is not None
    assert headers.get("X-Tenant-Id") == "tnt_01J5TEST00000000000000TNT1"
    assert body == {"title": "新文档"}


def test_get_canvas_proxy_happy(
    client: TestClient, authed_cookie: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeOrchestrator()
    fake.set_response(
        "GET",
        "/canvases/cvs_1",
        httpx.Response(
            200,
            json={"id": "cvs_1", "current_version": 3, "blocks": [{"id": "blk_1"}]},
        ),
    )
    _patch_orch(fake, monkeypatch)

    resp = client.get(f"{PREFIX}/canvases/cvs_1", cookies=authed_cookie)
    assert resp.status_code == 200
    assert resp.json()["current_version"] == 3

    get_calls = [c for c in fake.calls if c[0] == "GET" and c[1].endswith("/canvases/cvs_1")]
    assert len(get_calls) == 1


def test_list_canvases_proxy_happy(
    client: TestClient, authed_cookie: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeOrchestrator()
    fake.set_response(
        "GET",
        "/sessions/ses_abc/canvases",
        httpx.Response(200, json={"items": [{"id": "cvs_1"}]}),
    )
    _patch_orch(fake, monkeypatch)

    resp = client.get(f"{PREFIX}/sessions/ses_abc/canvases", cookies=authed_cookie)
    assert resp.status_code == 200
    assert resp.json() == {"items": [{"id": "cvs_1"}]}


def test_list_versions_proxy_happy(
    client: TestClient, authed_cookie: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeOrchestrator()
    fake.set_response(
        "GET",
        "/canvases/cvs_1/versions",
        httpx.Response(200, json={"items": [{"version": 1}, {"version": 2}]}),
    )
    _patch_orch(fake, monkeypatch)

    resp = client.get(f"{PREFIX}/canvases/cvs_1/versions", cookies=authed_cookie)
    assert resp.status_code == 200
    assert len(resp.json()["items"]) == 2


def test_get_version_proxy_happy(
    client: TestClient, authed_cookie: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeOrchestrator()
    fake.set_response(
        "GET",
        "/canvases/cvs_1/versions/2",
        httpx.Response(200, json={"version": 2, "blocks": []}),
    )
    _patch_orch(fake, monkeypatch)

    resp = client.get(f"{PREFIX}/canvases/cvs_1/versions/2", cookies=authed_cookie)
    assert resp.status_code == 200
    assert resp.json()["version"] == 2


def test_create_version_proxy_relays_body_and_409(
    client: TestClient, authed_cookie: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Manual-edit version creation may 409 on base_version conflict — relayed verbatim."""
    fake = FakeOrchestrator()
    fake.set_response(
        "POST",
        "/canvases/cvs_1/versions",
        httpx.Response(409, json={"detail": "version conflict", "user_message": "冲突"}),
    )
    _patch_orch(fake, monkeypatch)

    resp = client.post(
        f"{PREFIX}/canvases/cvs_1/versions",
        cookies=authed_cookie,
        json={"base_version": 1, "blocks": []},
    )
    assert resp.status_code == 409
    assert resp.json()["user_message"] == "冲突"


def test_revert_canvas_proxy_happy(
    client: TestClient, authed_cookie: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeOrchestrator()
    fake.set_response(
        "POST",
        "/canvases/cvs_1/revert",
        httpx.Response(200, json={"id": "cvs_1", "current_version": 5}),
    )
    _patch_orch(fake, monkeypatch)

    resp = client.post(
        f"{PREFIX}/canvases/cvs_1/revert",
        cookies=authed_cookie,
        json={"target_version": 2},
    )
    assert resp.status_code == 200
    assert resp.json()["current_version"] == 5


def test_accept_suggestion_proxy_happy(
    client: TestClient, authed_cookie: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeOrchestrator()
    fake.set_response(
        "POST",
        "/canvases/cvs_1/suggestions/csg_1/accept",
        httpx.Response(200, json={"id": "csg_1", "status": "accepted"}),
    )
    _patch_orch(fake, monkeypatch)

    resp = client.post(
        f"{PREFIX}/canvases/cvs_1/suggestions/csg_1/accept",
        cookies=authed_cookie,
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "accepted"


def test_reject_suggestion_proxy_relays_404(
    client: TestClient, authed_cookie: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Status passthrough for a non-2xx upstream response (suggestion not found)."""
    fake = FakeOrchestrator()
    fake.set_response(
        "POST",
        "/canvases/cvs_1/suggestions/csg_missing/reject",
        httpx.Response(404, json={"detail": "not found"}),
    )
    _patch_orch(fake, monkeypatch)

    resp = client.post(
        f"{PREFIX}/canvases/cvs_1/suggestions/csg_missing/reject",
        cookies=authed_cookie,
    )
    assert resp.status_code == 404
    assert resp.json() == {"detail": "not found"}


# ---------------------------------------------------------------------------
# Fake gateway/orchestrator for the edit endpoint's base_version pre-check
# ---------------------------------------------------------------------------

class FakeEditBackend:
    """Combined orchestrator + gateway stub for the v2 edit endpoint flow.

    Routes: GET /canvases/{cid}, GET /tenant-keys, GET /tenant-defaults/resolve,
    POST /chat/completions, POST /canvases/{cid}/suggestions.
    """

    def __init__(
        self,
        *,
        current_version: int = 2,
        blocks: list[dict] | None = None,
        completion_content: str | None = None,
    ) -> None:
        self.current_version = current_version
        self.blocks = (
            blocks
            if blocks is not None
            else [{"id": "blk_1", "type": "paragraph", "text": "旧内容"}]
        )
        self.completion_content = (
            completion_content
            if completion_content is not None
            else (
                '{"edits": [{"block_id": "blk_1", "op": "replace", '
                '"new_text": "新内容"}], "insertions": [], '
                '"change_summary": "改了"}'
            )
        )
        self.calls: list[tuple[str, str]] = []
        self.completion_call_count = 0

    def route(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        self.calls.append((method, url))

        if method == "GET" and url.rstrip("/").endswith(
            tuple(f"/canvases/{c}" for c in ["cvs_1"])
        ):
            return httpx.Response(
                200,
                json={
                    "id": "cvs_1",
                    "current_version": self.current_version,
                    "blocks": self.blocks,
                },
            )
        if method == "GET" and "/tenant-keys" in url:
            return httpx.Response(200, json=[{"virtual_key": "vk_test", "enabled": True}])
        if method == "GET" and "/tenant-defaults/resolve" in url:
            return httpx.Response(200, json={"purpose": "llm", "model_id": "deepseek-chat"})
        if method == "POST" and "/chat/completions" in url:
            self.completion_call_count += 1
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {"message": {"role": "assistant", "content": self.completion_content}}
                    ]
                },
            )
        if method == "POST" and "/suggestions" in url:
            return httpx.Response(
                201,
                json={
                    "id": "csg_1",
                    "canvas_id": "cvs_1",
                    "base_version": self.current_version,
                    "status": "proposed",
                    "instruction": "改",
                    "diffs": [
                        {
                            "block_id": "blk_1",
                            "op": "replace",
                            "orig": "旧内容",
                            "sugg": "新内容",
                        }
                    ],
                    "created_at": "2026-01-01T00:00:00Z",
                },
            )
        return httpx.Response(200, json={"ok": True})


def _patch_edit_backend(fake: FakeEditBackend, monkeypatch: pytest.MonkeyPatch) -> None:
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

        def stream(self, method: str, url: str, **kw: Any) -> Any:
            # The edit endpoint streams /chat/completions (SSE); re-encode the
            # fake's non-streaming completion body as SSE deltas.
            resp = fake.route(method, url, **kw)

            class _Stream:
                async def __aenter__(self_inner) -> httpx.Response:
                    if resp.status_code != 200:
                        return resp
                    content = resp.json()["choices"][0]["message"]["content"]
                    chunk = {"choices": [{"delta": {"content": content}}]}
                    sse = f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n"
                    return httpx.Response(200, content=sse.encode())

                async def __aexit__(self_inner, *a: Any) -> None:
                    pass

            return _Stream()

    monkeypatch.setattr(httpx, "AsyncClient", _AC)


def _edit_body(**over: Any) -> dict:
    base: dict[str, Any] = {
        "instruction": "改",
        "scope": {"type": "document"},
    }
    base.update(over)
    return base


# ---------------------------------------------------------------------------
# base_version optimistic-lock pre-check
# ---------------------------------------------------------------------------

def test_edit_stale_base_version_returns_409_without_llm(
    client: TestClient, authed_cookie: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeEditBackend(current_version=2)
    _patch_edit_backend(fake, monkeypatch)

    resp = client.post(
        f"{PREFIX}/sessions/ses_abc/canvases/cvs_1/edit",
        cookies=authed_cookie,
        json=_edit_body(base_version=1),
    )
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert "refresh" in detail["user_message"]

    assert fake.completion_call_count == 0
    assert not any("/chat/completions" in c[1] for c in fake.calls)


def test_edit_matching_base_version_proceeds(
    client: TestClient, authed_cookie: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeEditBackend(current_version=2)
    _patch_edit_backend(fake, monkeypatch)

    resp = client.post(
        f"{PREFIX}/sessions/ses_abc/canvases/cvs_1/edit",
        cookies=authed_cookie,
        json=_edit_body(base_version=2),
    )
    assert resp.status_code == 200
    assert fake.completion_call_count == 1
    assert any("/chat/completions" in c[1] for c in fake.calls)
    assert resp.json()["suggestion"]["id"] == "csg_1"


def test_edit_no_base_version_unchanged(
    client: TestClient, authed_cookie: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Backward compat: omitting base_version proceeds normally (no 409)."""
    fake = FakeEditBackend(current_version=2)
    _patch_edit_backend(fake, monkeypatch)

    resp = client.post(
        f"{PREFIX}/sessions/ses_abc/canvases/cvs_1/edit",
        cookies=authed_cookie,
        json=_edit_body(),
    )
    assert resp.status_code == 200
    assert fake.completion_call_count == 1
