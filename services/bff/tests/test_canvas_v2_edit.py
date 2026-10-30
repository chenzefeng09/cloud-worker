"""Contract tests for the BFF canvas v2 作用域编辑 endpoint.

Covers (设计 §5.2 / 任务清单):
1. auth guard → 401
2. happy path (scope=blocks): edits+insertions → diffs correct, orig back-filled,
   suggestion落库, 200 {suggestion}
3. scope=document: target = all blocks, prompt renders every block
4. invalid JSON on first attempt → retry succeeds
5. invalid JSON twice → 502 BAD_MODEL_OUTPUT
6. block_id out of scope (not in current version) → 422
7. gateway 402 → problem+json (502, upstream user_message surfaced)
8. orchestrator 409 on suggestion → 409 passthrough
9. request validation: scope=blocks with empty block_ids → 400
10. pure fns: build_diffs orig back-fill, parse_and_validate_output schema guards

Uses the same fake-gateway monkeypatch style as test_canvas_edit.py, with an added
fake orchestrator routed by URL.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from agent_bff.api.v1 import canvas as canvas_mod
from agent_bff.api.v1.canvas_v2 import (
    ModelOutputError,
    build_diffs,
    build_scoped_edit_prompt,
    build_selection_diffs,
    build_selection_edit_prompt,
    parse_and_validate_output,
    parse_selection_output,
)

PREFIX = "/api/bff/v1"

CANVAS_ID = "cvs_01J5TESTCANVAS00000000001"
SESSION_ID = "ses_abc"

# What the gateway answers a key mint for a tenant with no enabled LLM provider.
_NO_PROVIDER = {"code": "FOUNDATION.LLM_GATEWAY.PROVIDER.NOT_FOUND", "status": 404}

_DEFAULT_BLOCKS = [
    {"id": "blk_00000001", "type": "heading", "text": "# 标题"},
    {"id": "blk_00000002", "type": "paragraph", "text": "第一段原文内容。"},
    {"id": "blk_00000003", "type": "paragraph", "text": "第二段原文内容。"},
]


def _good_output(
    *,
    edits: list[dict] | None = None,
    insertions: list[dict] | None = None,
    change_summary: str = "把第二段改得更专业",
) -> str:
    return json.dumps(
        {
            "edits": edits
            if edits is not None
            else [
                {
                    "block_id": "blk_00000002",
                    "op": "replace",
                    "new_text": "第一段改写后的更专业内容。",
                }
            ],
            "insertions": insertions if insertions is not None else [],
            "change_summary": change_summary,
        },
        ensure_ascii=False,
    )


# ---------------------------------------------------------------------------
# Fake gateway + orchestrator (routed by URL through one AsyncClient patch)
# ---------------------------------------------------------------------------

class FakeBackends:
    """Stub for gateway (/tenant-keys/ephemeral, /tenant-keys, /tenant-defaults,
    /chat/completions) + orchestrator (GET /canvases/{id}, POST /canvases/{id}/suggestions)."""

    def __init__(
        self,
        *,
        mint_status: int = 201,
        mint_body: Any = None,
        virtual_keys: Any = None,
        default_model_id: str | None = "deepseek-chat",
        blocks: list[dict] | None = None,
        current_version: int = 3,
        canvas_status: int = 200,
        completion_contents: list[str] | None = None,
        completion_status: int = 200,
        completion_error: Exception | None = None,
        suggestion_status: int = 201,
        suggestion_body: dict | None = None,
    ) -> None:
        self.mint_status = mint_status
        self.mint_body = mint_body if mint_body is not None else {"virtual_key": "vk_eph"}
        self.virtual_keys = (
            virtual_keys
            if virtual_keys is not None
            else [{"virtual_key": "vk_test", "enabled": True}]
        )
        self.default_model_id = default_model_id
        self.blocks = blocks if blocks is not None else list(_DEFAULT_BLOCKS)
        self.current_version = current_version
        self.canvas_status = canvas_status
        # A queue of completion contents; each POST /chat/completions pops one.
        self.completion_contents = (
            completion_contents
            if completion_contents is not None
            else [_good_output()]
        )
        self._completion_idx = 0
        self.completion_status = completion_status
        self.completion_error = completion_error
        self.suggestion_status = suggestion_status
        self.suggestion_body = suggestion_body

        self.calls: list[tuple[str, str, dict | None]] = []
        self.last_completion_payload: dict | None = None
        self.last_completion_headers: dict[str, str] | None = None
        self.completion_payloads: list[dict] = []
        self.last_suggestion_body: dict | None = None

    def route(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        self.calls.append((method, url, kwargs.get("json")))

        # ── gateway meta ──
        if url.endswith("/tenant-keys/ephemeral") and method == "POST":
            return httpx.Response(self.mint_status, json=self.mint_body)
        if "/tenant-keys" in url and method == "GET":
            return httpx.Response(200, json=self.virtual_keys)
        if "/tenant-defaults/resolve" in url and method == "GET":
            if self.default_model_id is None:
                return httpx.Response(200, json={"purpose": "llm"})
            return httpx.Response(
                200, json={"purpose": "llm", "model_id": self.default_model_id}
            )

        # ── gateway completion ──
        if "/chat/completions" in url and method == "POST":
            if self.completion_error is not None:
                raise self.completion_error
            self.last_completion_headers = kwargs.get("headers")
            self.last_completion_payload = kwargs.get("json")
            self.completion_payloads.append(kwargs.get("json"))
            if self.completion_status != 200:
                return httpx.Response(
                    self.completion_status,
                    json={"user_message": "配额不足", "detail": "quota exceeded"},
                )
            idx = min(self._completion_idx, len(self.completion_contents) - 1)
            content = self.completion_contents[idx]
            self._completion_idx += 1
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {"message": {"role": "assistant", "content": content}}
                    ]
                },
            )

        # ── orchestrator: GET canvas detail ──
        if url.endswith(f"/canvases/{CANVAS_ID}") and method == "GET":
            if self.canvas_status != 200:
                return httpx.Response(self.canvas_status, json={"detail": "nope"})
            return httpx.Response(
                200,
                json={
                    "id": CANVAS_ID,
                    "session_id": SESSION_ID,
                    "kind": "article",
                    "title": "测试画布",
                    "current_version": self.current_version,
                    "blocks": self.blocks,
                    "updated_at": "2026-07-10T00:00:00Z",
                },
            )

        # ── orchestrator: POST suggestion ──
        if url.endswith(f"/canvases/{CANVAS_ID}/suggestions") and method == "POST":
            self.last_suggestion_body = kwargs.get("json")
            if self.suggestion_status == 409:
                return httpx.Response(
                    409,
                    json={"user_message": "画布已被更新", "detail": "version conflict"},
                )
            if self.suggestion_status not in (200, 201):
                return httpx.Response(self.suggestion_status, json={"detail": "err"})
            body = self.suggestion_body or {
                "id": "csg_01J5TESTSUGGESTION0000001",
                "canvas_id": CANVAS_ID,
                "base_version": self.current_version,
                "status": "proposed",
                "instruction": (kwargs.get("json") or {}).get("instruction"),
                "diffs": (kwargs.get("json") or {}).get("diffs"),
                "change_summary": (kwargs.get("json") or {}).get("change_summary"),
                "created_at": "2026-07-10T00:00:01Z",
            }
            return httpx.Response(self.suggestion_status, json=body)

        return httpx.Response(200, json={"ok": True})


def _patch(fake: FakeBackends, monkeypatch: pytest.MonkeyPatch) -> None:
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


@pytest.fixture(autouse=True)
def _clear_vkey_cache() -> None:
    canvas_mod._vkey_cache.clear()


def _url(canvas_id: str = CANVAS_ID) -> str:
    return f"{PREFIX}/sessions/{SESSION_ID}/canvases/{canvas_id}/edit"


def _body(**over: Any) -> dict:
    base: dict[str, Any] = {
        "instruction": "把第二段改得更专业一些",
        "scope": {"type": "blocks", "block_ids": ["blk_00000002"]},
        "model": "deepseek-chat",
    }
    base.update(over)
    return base


def _selection_body(**over: Any) -> dict:
    base = _body(
        instruction="把选中的两个字改得更专业",
        scope={
            "type": "selection",
            "segments": [
                {
                    "block_id": "blk_00000002",
                    "start": 3,
                    "end": 5,
                    "text": "原文",
                }
            ],
        },
    )
    base.update(over)
    return base


# ---------------------------------------------------------------------------
# Auth guard
# ---------------------------------------------------------------------------

def test_canvas_v2_requires_auth(client: TestClient) -> None:
    resp = client.post(_url(), json=_body())
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Happy path (scope=blocks)
# ---------------------------------------------------------------------------

def test_canvas_v2_happy_blocks(
    client: TestClient, authed_cookie: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeBackends(
        completion_contents=[
            _good_output(
                edits=[
                    {
                        "block_id": "blk_00000002",
                        "op": "replace",
                        "new_text": "第一段改写后的更专业内容。",
                    }
                ],
                insertions=[
                    {"after_block_id": "blk_00000002", "new_text": "这是一段新插入的补充。"}
                ],
                change_summary="改写第二段并补充一段",
            )
        ]
    )
    _patch(fake, monkeypatch)

    resp = client.post(_url(), cookies=authed_cookie, json=_body())
    assert resp.status_code == 200, resp.json()
    suggestion = resp.json()["suggestion"]
    assert suggestion["id"] == "csg_01J5TESTSUGGESTION0000001"
    assert suggestion["base_version"] == 3

    # diffs换算正确 + orig回填正确（从当前 blocks 拿）。
    diffs = fake.last_suggestion_body["diffs"]
    replace_diff = next(d for d in diffs if d.get("op") == "replace")
    assert replace_diff["block_id"] == "blk_00000002"
    assert replace_diff["orig"] == "第一段原文内容。"  # back-filled from current blocks
    assert replace_diff["sugg"] == "第一段改写后的更专业内容。"

    insert_diff = next(d for d in diffs if d.get("op") == "insert_after")
    assert insert_diff["block_id"] == "blk_00000002"
    assert insert_diff["orig"] is None
    assert insert_diff["sugg"] == "这是一段新插入的补充。"

    # suggestion body carries base_version + instruction + change_summary.
    assert fake.last_suggestion_body["base_version"] == 3
    assert fake.last_suggestion_body["instruction"] == "把第二段改得更专业一些"
    assert fake.last_suggestion_body["change_summary"] == "改写第二段并补充一段"

    # gateway called with virtual key + json_object response_format + model.
    assert fake.last_completion_headers.get("X-LLM-Virtual-Key") == "vk_eph"
    assert fake.last_completion_payload["model"] == "deepseek-chat"
    assert fake.last_completion_payload["response_format"] == {"type": "json_object"}
    prompt = fake.last_completion_payload["messages"][0]["content"]
    # target block rendered as可改.
    assert "[blk_00000002] [EDITABLE]" in prompt
    # neighbour blocks rendered as read-only context.
    assert "[blk_00000001] [READ-ONLY CONTEXT]" in prompt
    assert "[blk_00000003] [READ-ONLY CONTEXT]" in prompt

    # no runs / messages touched.
    assert not any("/runs" in u or "/messages" in u for _, u, _ in fake.calls)


def test_canvas_v2_happy_character_selection(
    client: TestClient, authed_cookie: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeBackends(
        completion_contents=[
            json.dumps(
                {
                    "replacements": [
                        {"block_id": "blk_00000002", "new_text": "专业表述"}
                    ],
                    "change_summary": "局部改写两个字",
                },
                ensure_ascii=False,
            )
        ]
    )
    _patch(fake, monkeypatch)

    resp = client.post(_url(), cookies=authed_cookie, json=_selection_body())
    assert resp.status_code == 200, resp.json()

    prompt = fake.last_completion_payload["messages"][0]["content"]
    assert '"selected_text": "原文"' in prompt
    assert "do not return whole paragraphs or the whole document" in prompt

    diff = fake.last_suggestion_body["diffs"][0]
    assert diff["orig"] == "第一段原文内容。"
    assert diff["sugg"] == "第一段专业表述内容。"
    assert diff["selection_start"] == 3
    assert diff["selection_end"] == 5
    assert diff["selected_orig"] == "原文"
    assert diff["selected_sugg"] == "专业表述"


def test_canvas_v2_rejects_stale_selected_text_before_model_call(
    client: TestClient, authed_cookie: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeBackends()
    _patch(fake, monkeypatch)

    resp = client.post(
        _url(),
        cookies=authed_cookie,
        json=_selection_body(
            scope={
                "type": "selection",
                "segments": [
                    {
                        "block_id": "blk_00000002",
                        "start": 3,
                        "end": 5,
                        "text": "旧文",
                    }
                ],
            }
        ),
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["detail"]["code"] == "AGENT.BFF.CANVAS.SELECTION_STALE"
    assert not any("/chat/completions" in url for _, url, _ in fake.calls)


# ---------------------------------------------------------------------------
# scope=document
# ---------------------------------------------------------------------------

def test_canvas_v2_document_scope(
    client: TestClient, authed_cookie: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeBackends(
        completion_contents=[
            _good_output(
                edits=[
                    {"block_id": "blk_00000001", "op": "replace", "new_text": "# 新标题"},
                    {"block_id": "blk_00000003", "op": "delete"},
                ]
            )
        ]
    )
    _patch(fake, monkeypatch)

    resp = client.post(
        _url(),
        cookies=authed_cookie,
        json=_body(scope={"type": "document"}),
    )
    assert resp.status_code == 200, resp.json()

    # document scope → all blocks are targets; each appears (as可改) in the prompt.
    prompt = fake.last_completion_payload["messages"][0]["content"]
    for b in _DEFAULT_BLOCKS:
        assert b["id"] in prompt
        # every rendered block line carries the可改 role, none is read-only context.
        assert f"[{b['id']}] [EDITABLE]" in prompt
        assert f"[{b['id']}] [READ-ONLY CONTEXT]" not in prompt

    diffs = fake.last_suggestion_body["diffs"]
    delete_diff = next(d for d in diffs if d.get("op") == "delete")
    assert delete_diff["block_id"] == "blk_00000003"
    assert delete_diff["orig"] == "第二段原文内容。"
    assert delete_diff["sugg"] is None


# ---------------------------------------------------------------------------
# Retry on invalid JSON
# ---------------------------------------------------------------------------

def test_canvas_v2_retry_then_success(
    client: TestClient, authed_cookie: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeBackends(
        completion_contents=[
            "这不是 JSON，只是一段散文。",  # first attempt invalid
            _good_output(),  # retry valid
        ]
    )
    _patch(fake, monkeypatch)

    resp = client.post(_url(), cookies=authed_cookie, json=_body())
    assert resp.status_code == 200, resp.json()

    # gateway consulted twice; retry prompt carries the validation error.
    completion_calls = [c for c in fake.calls if "/chat/completions" in c[1]]
    assert len(completion_calls) == 2
    retry_prompt = fake.completion_payloads[1]["messages"][0]["content"]
    assert "failed validation" in retry_prompt


def test_canvas_v2_retry_exhausted_502(
    client: TestClient, authed_cookie: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeBackends(
        completion_contents=["还是散文一", "还是散文二"]  # both invalid
    )
    _patch(fake, monkeypatch)

    resp = client.post(_url(), cookies=authed_cookie, json=_body())
    assert resp.status_code == 502
    detail = resp.json()["detail"]
    assert detail["detail"]["code"] == "AGENT.BFF.CANVAS.BAD_MODEL_OUTPUT"
    # exactly two gateway attempts, no suggestion落库.
    completion_calls = [c for c in fake.calls if "/chat/completions" in c[1]]
    assert len(completion_calls) == 2
    assert not any("/suggestions" in u for _, u, _ in fake.calls)


# ---------------------------------------------------------------------------
# block_id out of scope
# ---------------------------------------------------------------------------

def test_canvas_v2_block_id_not_found_422(
    client: TestClient, authed_cookie: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeBackends()
    _patch(fake, monkeypatch)

    resp = client.post(
        _url(),
        cookies=authed_cookie,
        json=_body(scope={"type": "blocks", "block_ids": ["blk_DOESNOTEXIST"]}),
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["detail"]["code"] == "AGENT.BFF.CANVAS.BLOCK_NOT_FOUND"
    # never reached the gateway.
    assert not any("/chat/completions" in u for _, u, _ in fake.calls)


# ---------------------------------------------------------------------------
# Request validation: scope=blocks needs non-empty block_ids
# ---------------------------------------------------------------------------

def test_canvas_v2_blocks_requires_block_ids_400(
    client: TestClient, authed_cookie: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeBackends()
    _patch(fake, monkeypatch)

    resp = client.post(
        _url(),
        cookies=authed_cookie,
        json=_body(scope={"type": "blocks", "block_ids": []}),
    )
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert detail["detail"]["code"] == "AGENT.BFF.CANVAS.NO_BLOCK_IDS"


# ---------------------------------------------------------------------------
# gateway 402 → problem
# ---------------------------------------------------------------------------

def test_canvas_v2_gateway_402_problem(
    client: TestClient, authed_cookie: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeBackends(completion_status=402)
    _patch(fake, monkeypatch)

    resp = client.post(_url(), cookies=authed_cookie, json=_body())
    assert resp.status_code == 502
    detail = resp.json()["detail"]
    assert detail["detail"]["code"] == "AGENT.BFF.CANVAS.GATEWAY_ERROR"
    # upstream user_message surfaced.
    assert detail["user_message"] == "配额不足"


# ---------------------------------------------------------------------------
# orchestrator 404 (canvas gone) → 404
# ---------------------------------------------------------------------------

def test_canvas_v2_canvas_not_found_404(
    client: TestClient, authed_cookie: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeBackends(canvas_status=404)
    _patch(fake, monkeypatch)

    resp = client.post(_url(), cookies=authed_cookie, json=_body())
    assert resp.status_code == 404
    detail = resp.json()["detail"]
    assert detail["detail"]["code"] == "AGENT.BFF.CANVAS.NOT_FOUND"


# ---------------------------------------------------------------------------
# orchestrator 409 on suggestion → 409 passthrough
# ---------------------------------------------------------------------------

def test_canvas_v2_suggestion_409_passthrough(
    client: TestClient, authed_cookie: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeBackends(suggestion_status=409)
    _patch(fake, monkeypatch)

    resp = client.post(_url(), cookies=authed_cookie, json=_body())
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["detail"]["code"] == "AGENT.BFF.CANVAS.VERSION_CONFLICT"
    assert detail["user_message"] == "画布已被更新"


# ---------------------------------------------------------------------------
# no model key: no provider → 422; nothing obtainable → 502
# ---------------------------------------------------------------------------

def test_canvas_v2_tenant_without_llm_provider_422(
    client: TestClient, authed_cookie: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeBackends(mint_status=404, mint_body=_NO_PROVIDER)
    _patch(fake, monkeypatch)

    resp = client.post(_url(), cookies=authed_cookie, json=_body())
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["detail"]["code"] == "AGENT.BFF.CANVAS.LLM_NOT_CONFIGURED"
    assert "Admin → Models" in detail["user_message"]
    assert fake.last_completion_payload is None


def test_canvas_v2_no_virtual_key_at_all_502(
    client: TestClient, authed_cookie: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeBackends(mint_status=503, mint_body={"detail": "down"}, virtual_keys=[])
    _patch(fake, monkeypatch)

    resp = client.post(_url(), cookies=authed_cookie, json=_body())
    assert resp.status_code == 502
    assert resp.json()["detail"]["detail"]["code"] == "AGENT.BFF.CANVAS.GATEWAY_UNAVAILABLE"


def test_canvas_v2_resolves_default_model_when_omitted(
    client: TestClient, authed_cookie: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeBackends(default_model_id="tenant-default-model")
    _patch(fake, monkeypatch)

    resp = client.post(_url(), cookies=authed_cookie, json=_body(model=None))
    assert resp.status_code == 200, resp.json()
    assert fake.last_completion_payload["model"] == "tenant-default-model"
    assert any("/tenant-defaults/resolve" in u for _, u, _ in fake.calls)


# ---------------------------------------------------------------------------
# Pure fn: build_diffs
# ---------------------------------------------------------------------------

def test_build_diffs_backfills_orig() -> None:
    blocks = [
        {"id": "b1", "text": "原文一"},
        {"id": "b2", "text": "原文二"},
    ]
    edits = [
        {"block_id": "b1", "op": "replace", "new_text": "新一"},
        {"block_id": "b2", "op": "delete"},
    ]
    insertions = [{"after_block_id": "b1", "new_text": "插入内容"}]
    diffs = build_diffs(edits=edits, insertions=insertions, blocks=blocks)

    assert diffs[0] == {
        "block_id": "b1",
        "op": "replace",
        "orig": "原文一",
        "sugg": "新一",
    }
    assert diffs[1] == {
        "block_id": "b2",
        "op": "delete",
        "orig": "原文二",
        "sugg": None,
    }
    assert diffs[2] == {
        "block_id": "b1",
        "op": "insert_after",
        "orig": None,
        "sugg": "插入内容",
    }


def test_selection_helpers_splice_only_the_verified_range() -> None:
    blocks = [{"id": "b1", "text": "前缀原文后缀"}]
    segments = [{"block_id": "b1", "start": 2, "end": 4, "text": "原文"}]
    replacements, summary = parse_selection_output(
        json.dumps(
            {
                "replacements": [{"block_id": "b1", "new_text": "新句"}],
                "change_summary": "局部替换",
            },
            ensure_ascii=False,
        ),
        target_ids=["b1"],
    )
    diffs = build_selection_diffs(
        replacements=replacements, segments=segments, blocks=blocks
    )
    assert summary == "局部替换"
    assert diffs == [
        {
            "block_id": "b1",
            "op": "replace",
            "orig": "前缀原文后缀",
            "sugg": "前缀新句后缀",
            "selection_start": 2,
            "selection_end": 4,
            "selected_orig": "原文",
            "selected_sugg": "新句",
        }
    ]

    prompt = build_selection_edit_prompt(
        instruction="改写", blocks=blocks, segments=segments
    )
    assert '"before_context": "前缀"' in prompt
    assert '"after_context": "后缀"' in prompt


# ---------------------------------------------------------------------------
# Pure fn: parse_and_validate_output
# ---------------------------------------------------------------------------

def test_parse_output_happy() -> None:
    content = _good_output()
    edits, insertions, summary = parse_and_validate_output(
        content, target_ids=["blk_00000002"]
    )
    assert edits == [
        {
            "block_id": "blk_00000002",
            "op": "replace",
            "new_text": "第一段改写后的更专业内容。",
        }
    ]
    assert insertions == []
    assert summary == "把第二段改得更专业"


def test_parse_output_strips_json_fence() -> None:
    content = "```json\n" + _good_output() + "\n```"
    edits, _, _ = parse_and_validate_output(content, target_ids=["blk_00000002"])
    assert edits[0]["block_id"] == "blk_00000002"


def test_parse_output_rejects_out_of_scope_block() -> None:
    content = _good_output(
        edits=[{"block_id": "blk_OTHER", "op": "replace", "new_text": "x"}]
    )
    with pytest.raises(ModelOutputError):
        parse_and_validate_output(content, target_ids=["blk_00000002"])


def test_parse_output_rejects_replace_without_new_text() -> None:
    content = json.dumps(
        {"edits": [{"block_id": "blk_00000002", "op": "replace"}], "insertions": []}
    )
    with pytest.raises(ModelOutputError):
        parse_and_validate_output(content, target_ids=["blk_00000002"])


def test_parse_output_rejects_bad_op() -> None:
    content = json.dumps(
        {"edits": [{"block_id": "blk_00000002", "op": "rewrite", "new_text": "x"}]}
    )
    with pytest.raises(ModelOutputError):
        parse_and_validate_output(content, target_ids=["blk_00000002"])


def test_parse_output_rejects_empty_edits_and_insertions() -> None:
    content = json.dumps({"edits": [], "insertions": []})
    with pytest.raises(ModelOutputError):
        parse_and_validate_output(content, target_ids=["blk_00000002"])


def test_parse_output_rejects_non_json() -> None:
    with pytest.raises(ModelOutputError):
        parse_and_validate_output("这是散文", target_ids=["blk_00000002"])


def test_parse_output_delete_needs_no_new_text() -> None:
    content = json.dumps(
        {"edits": [{"block_id": "blk_00000002", "op": "delete"}], "insertions": []}
    )
    edits, insertions, _ = parse_and_validate_output(
        content, target_ids=["blk_00000002"]
    )
    assert edits == [{"block_id": "blk_00000002", "op": "delete"}]


# ---------------------------------------------------------------------------
# Pure fn: build_scoped_edit_prompt
# ---------------------------------------------------------------------------

def test_build_prompt_marks_target_and_context() -> None:
    prompt = build_scoped_edit_prompt(
        instruction="改第二段",
        blocks=_DEFAULT_BLOCKS,
        target_ids=["blk_00000002"],
    )
    assert "Edit request: 改第二段" in prompt
    assert "[blk_00000002] [EDITABLE]" in prompt
    # neighbours of block 2 are blocks 1 and 3, rendered read-only.
    assert "[blk_00000001] [READ-ONLY CONTEXT]" in prompt
    assert "[blk_00000003] [READ-ONLY CONTEXT]" in prompt


def test_build_prompt_retry_error_prepended() -> None:
    prompt = build_scoped_edit_prompt(
        instruction="改第二段",
        blocks=_DEFAULT_BLOCKS,
        target_ids=["blk_00000002"],
        retry_error="edits[0].op 非法",
    )
    assert "failed validation" in prompt
    assert "edits[0].op 非法" in prompt
