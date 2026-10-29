"""Tests for the BFF canvas 局部修订 endpoint.

Covers:
1. POST /sessions/{id}/canvas/edit — unauthenticated → 401
2. happy path (selection) → {replacement, summary}, chat/completions called
3. happy path (document scope) → whole-doc prompt
4. request model wins; no model → resolve tenant default
5. no model + no tenant default → 400 with user_message
6. no virtual key → 400 with user_message
7. gateway non-200 → 502 with user_message
8. gateway timeout → 502 with user_message
9. empty replacement → 502 with user_message
10. tag parsing: with tags, no-tag fallback, code-fence stripping, missing instruction
11. virtual-key cache TTL behaviour
12. prompt builder semantics
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from agent_bff.api.v1 import canvas as canvas_mod
from agent_bff.api.v1.canvas import (
    CanvasEditRequest,
    build_canvas_edit_prompt,
    normalize_selection_replacement,
    parse_canvas_response,
)

PREFIX = "/api/bff/v1"


# ---------------------------------------------------------------------------
# Fake gateway supporting the full workflow
# ---------------------------------------------------------------------------

class FakeGateway:
    """Stub for the multi-step canvas edit workflow.

    Handles GET /tenant-keys, GET /tenant-defaults/resolve, POST /chat/completions.
    """

    def __init__(
        self,
        *,
        virtual_keys: Any = None,
        default_model_id: str | None = "deepseek-chat",
        completion_content: str | None = None,
        completion_status: int = 200,
        completion_error: Exception | None = None,
    ) -> None:
        # None → return empty list (no key). Otherwise the raw JSON body.
        self.virtual_keys = (
            virtual_keys
            if virtual_keys is not None
            else [{"virtual_key": "vk_test", "enabled": True}]
        )
        self.default_model_id = default_model_id
        self.completion_content = (
            completion_content
            if completion_content is not None
            else (
                "这里是前言。\n"
                "<canvas-replacement>\n改好的更专业的文本\n</canvas-replacement>\n"
                "<change-summary>\n- 用词更专业\n</change-summary>"
            )
        )
        self.completion_status = completion_status
        self.completion_error = completion_error
        self.calls: list[tuple[str, str, dict | None]] = []
        self.last_completion_headers: dict[str, str] | None = None
        self.last_completion_payload: dict | None = None

    def route(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        self.calls.append((method, url, kwargs.get("headers")))
        if "/tenant-keys" in url and method == "GET":
            return httpx.Response(200, json=self.virtual_keys)
        if "/tenant-defaults/resolve" in url and method == "GET":
            if self.default_model_id is None:
                return httpx.Response(200, json={"purpose": "llm"})
            return httpx.Response(
                200, json={"purpose": "llm", "model_id": self.default_model_id}
            )
        if "/chat/completions" in url and method == "POST":
            if self.completion_error is not None:
                raise self.completion_error
            self.last_completion_headers = kwargs.get("headers")
            self.last_completion_payload = kwargs.get("json")
            if self.completion_status != 200:
                return httpx.Response(
                    self.completion_status,
                    json={"user_message": "上游拒绝", "detail": "boom"},
                )
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {"message": {"role": "assistant", "content": self.completion_content}}
                    ]
                },
            )
        return httpx.Response(200, json={"ok": True})


def _patch(fake: FakeGateway, monkeypatch: pytest.MonkeyPatch) -> None:
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


@pytest.fixture(autouse=True)
def _clear_vkey_cache() -> None:
    canvas_mod._vkey_cache.clear()


def _body(**over: Any) -> dict:
    base = {
        "title": "文章标题",
        "content": "画布全文。这句需要更专业。收尾。",
        "scope": "selection",
        "selection": {"text": "这句需要更专业", "start": 5, "end": 12},
        "instruction": "把这句改得更专业一些",
        "model": "deepseek-chat",
    }
    base.update(over)
    return base


# ---------------------------------------------------------------------------
# Auth guard
# ---------------------------------------------------------------------------

def test_canvas_edit_requires_auth(client: TestClient) -> None:
    resp = client.post(f"{PREFIX}/sessions/ses_abc/canvas/edit", json=_body())
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------

def test_canvas_edit_happy_selection(
    client: TestClient, authed_cookie: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeGateway()
    _patch(fake, monkeypatch)

    resp = client.post(
        f"{PREFIX}/sessions/ses_abc/canvas/edit", cookies=authed_cookie, json=_body()
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["replacement"] == "改好的更专业的文本"
    assert body["summary"] == "- 用词更专业"
    # A well-behaved clean fragment stays a selection-scoped apply.
    assert body["apply_to"] == "selection"

    # chat/completions must have been called with the virtual key header + model.
    assert fake.last_completion_headers is not None
    assert fake.last_completion_headers.get("X-LLM-Virtual-Key") == "vk_test"
    assert fake.last_completion_payload is not None
    assert fake.last_completion_payload["model"] == "deepseek-chat"
    assert fake.last_completion_payload["stream"] is False
    prompt = fake.last_completion_payload["messages"][0]["content"]
    assert "Edit only the selected fragment" in prompt
    assert "characters 5-12" in prompt

    # No run / message endpoints touched.
    assert not any("/runs" in u or "/messages" in u for _, u, _ in fake.calls)


def test_canvas_edit_document_scope(
    client: TestClient, authed_cookie: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeGateway()
    _patch(fake, monkeypatch)

    resp = client.post(
        f"{PREFIX}/sessions/ses_abc/canvas/edit",
        cookies=authed_cookie,
        json=_body(scope="document", selection=None),
    )
    assert resp.status_code == 200
    prompt = fake.last_completion_payload["messages"][0]["content"]  # type: ignore[index]
    assert "Edit the whole document" in prompt
    assert "Full document" in prompt


def test_canvas_edit_resolves_default_model_when_omitted(
    client: TestClient, authed_cookie: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeGateway(default_model_id="tenant-default-model")
    _patch(fake, monkeypatch)

    resp = client.post(
        f"{PREFIX}/sessions/ses_abc/canvas/edit",
        cookies=authed_cookie,
        json=_body(model=None),
    )
    assert resp.status_code == 200
    assert fake.last_completion_payload["model"] == "tenant-default-model"  # type: ignore[index]
    # resolve endpoint must have been consulted.
    assert any("/tenant-defaults/resolve" in u for _, u, _ in fake.calls)


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------

def test_canvas_edit_no_virtual_key_400(
    client: TestClient, authed_cookie: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeGateway(virtual_keys=[])
    _patch(fake, monkeypatch)

    resp = client.post(
        f"{PREFIX}/sessions/ses_abc/canvas/edit", cookies=authed_cookie, json=_body()
    )
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "model key" in detail["user_message"]


def test_canvas_edit_no_model_no_default_400(
    client: TestClient, authed_cookie: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeGateway(default_model_id=None)
    _patch(fake, monkeypatch)

    resp = client.post(
        f"{PREFIX}/sessions/ses_abc/canvas/edit",
        cookies=authed_cookie,
        json=_body(model=None),
    )
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "default model" in detail["user_message"]


def test_canvas_edit_missing_instruction_400(
    client: TestClient, authed_cookie: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeGateway()
    _patch(fake, monkeypatch)

    resp = client.post(
        f"{PREFIX}/sessions/ses_abc/canvas/edit",
        cookies=authed_cookie,
        json=_body(instruction="   "),
    )
    assert resp.status_code == 400
    assert "edit request" in resp.json()["detail"]["user_message"]


def test_canvas_edit_gateway_error_502(
    client: TestClient, authed_cookie: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeGateway(completion_status=500)
    _patch(fake, monkeypatch)

    resp = client.post(
        f"{PREFIX}/sessions/ses_abc/canvas/edit", cookies=authed_cookie, json=_body()
    )
    assert resp.status_code == 502
    detail = resp.json()["detail"]
    # upstream user_message is surfaced when present.
    assert detail["user_message"] == "上游拒绝"


def test_canvas_edit_gateway_timeout_502(
    client: TestClient, authed_cookie: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeGateway(completion_error=httpx.TimeoutException("slow"))
    _patch(fake, monkeypatch)

    resp = client.post(
        f"{PREFIX}/sessions/ses_abc/canvas/edit", cookies=authed_cookie, json=_body()
    )
    assert resp.status_code == 502
    assert "timed out" in resp.json()["detail"]["user_message"]


def test_canvas_edit_empty_replacement_502(
    client: TestClient, authed_cookie: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeGateway(
        completion_content="<canvas-replacement>\n   \n</canvas-replacement>"
    )
    _patch(fake, monkeypatch)

    resp = client.post(
        f"{PREFIX}/sessions/ses_abc/canvas/edit", cookies=authed_cookie, json=_body()
    )
    assert resp.status_code == 502
    assert "valid revision" in resp.json()["detail"]["user_message"]


def test_canvas_edit_no_tag_fallback(
    client: TestClient, authed_cookie: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """No replacement tag → whole reply (minus summary tag) becomes replacement."""
    fake = FakeGateway(
        completion_content="裸文本修订结果\n<change-summary>\n- 说明\n</change-summary>"
    )
    _patch(fake, monkeypatch)

    resp = client.post(
        f"{PREFIX}/sessions/ses_abc/canvas/edit", cookies=authed_cookie, json=_body()
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["replacement"] == "裸文本修订结果"
    assert body["summary"] == "- 说明"


# ---------------------------------------------------------------------------
# Pure function: prompt builder
# ---------------------------------------------------------------------------

def test_build_prompt_selection() -> None:
    req = CanvasEditRequest.model_validate(_body())
    prompt = build_canvas_edit_prompt(req)
    assert "As a professional editor" in prompt
    assert "Title: 文章标题" in prompt
    assert "Scope: Edit only the selected fragment" in prompt
    assert "Edit request: 把这句改得更专业一些" in prompt
    assert "Selected fragment (characters 5-12)" in prompt
    assert "<canvas-replacement>" in prompt
    assert "<change-summary>" in prompt


def test_build_prompt_document() -> None:
    req = CanvasEditRequest.model_validate(_body(scope="document", selection=None))
    prompt = build_canvas_edit_prompt(req)
    assert "Scope: Edit the whole document" in prompt
    assert "Full document:" in prompt
    assert "画布全文" in prompt


def test_build_prompt_default_title() -> None:
    req = CanvasEditRequest.model_validate(_body(title=""))
    prompt = build_canvas_edit_prompt(req)
    assert "Title: Untitled document" in prompt


def test_build_prompt_selection_forbids_echoing_context() -> None:
    """Selection-scoped prompt must forbid returning full text / surrounding原文."""
    req = CanvasEditRequest.model_validate(_body())
    prompt = build_canvas_edit_prompt(req)
    assert "only the rewrite of the selected fragment" in prompt
    assert "never return the whole document" in prompt
    assert "outside the selection" in prompt


# ---------------------------------------------------------------------------
# Pure function: response parsing
# ---------------------------------------------------------------------------

def test_parse_with_tags() -> None:
    content = (
        "开场白\n<canvas-replacement>\n最终文本\n</canvas-replacement>\n"
        "<change-summary>\n- 改动1\n- 改动2\n</change-summary>"
    )
    repl, summary = parse_canvas_response(content)
    assert repl == "最终文本"
    assert summary == "- 改动1\n- 改动2"


def test_parse_case_insensitive_tags() -> None:
    content = "<CANVAS-REPLACEMENT>大写标签</CANVAS-REPLACEMENT>"
    repl, summary = parse_canvas_response(content)
    assert repl == "大写标签"
    assert summary is None


def test_parse_strips_code_fence() -> None:
    content = (
        "<canvas-replacement>\n```markdown\n# 标题\n正文\n```\n</canvas-replacement>"
    )
    repl, _ = parse_canvas_response(content)
    assert repl == "# 标题\n正文"


def test_parse_no_tag_fallback_strips_summary() -> None:
    content = "纯文本回复\n<change-summary>\n- s\n</change-summary>"
    repl, summary = parse_canvas_response(content)
    assert repl == "纯文本回复"
    assert summary == "- s"


def test_parse_empty_summary_is_none() -> None:
    content = "<canvas-replacement>x</canvas-replacement><change-summary>  </change-summary>"
    repl, summary = parse_canvas_response(content)
    assert repl == "x"
    assert summary is None


# ---------------------------------------------------------------------------
# Virtual-key cache
# ---------------------------------------------------------------------------

def test_canvas_edit_vkey_cache_reuses_across_requests(
    client: TestClient, authed_cookie: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Second request within TTL must not re-query /tenant-keys."""
    fake = FakeGateway()
    _patch(fake, monkeypatch)

    r1 = client.post(
        f"{PREFIX}/sessions/ses_abc/canvas/edit", cookies=authed_cookie, json=_body()
    )
    assert r1.status_code == 200
    first_key_calls = [c for c in fake.calls if "/tenant-keys" in c[1]]
    assert len(first_key_calls) == 1

    r2 = client.post(
        f"{PREFIX}/sessions/ses_abc/canvas/edit", cookies=authed_cookie, json=_body()
    )
    assert r2.status_code == 200
    # Still only one tenant-keys call total — cache hit on the second request.
    all_key_calls = [c for c in fake.calls if "/tenant-keys" in c[1]]
    assert len(all_key_calls) == 1


def test_canvas_edit_vkey_cache_expires(
    client: TestClient, authed_cookie: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Expired cache entry forces a re-query."""
    fake = FakeGateway()
    _patch(fake, monkeypatch)

    r1 = client.post(
        f"{PREFIX}/sessions/ses_abc/canvas/edit", cookies=authed_cookie, json=_body()
    )
    assert r1.status_code == 200

    # Force-expire the cached entry.
    tid = "tnt_01J5TEST00000000000000TNT1"
    key, _ = canvas_mod._vkey_cache[tid]
    canvas_mod._vkey_cache[tid] = (key, 0.0)

    r2 = client.post(
        f"{PREFIX}/sessions/ses_abc/canvas/edit", cookies=authed_cookie, json=_body()
    )
    assert r2.status_code == 200
    key_calls = [c for c in fake.calls if "/tenant-keys" in c[1]]
    assert len(key_calls) == 2


# ---------------------------------------------------------------------------
# Pure function: replacement normalization (overlap trimming + doc detection)
# ---------------------------------------------------------------------------

def test_normalize_trims_echoed_surrounding_paragraph() -> None:
    """Model echoes half-sentences of原文 around the rewrite → trim to clean中间."""
    content = "前面是第一段完整内容。中间这句要改写得更专业。后面还有结尾的收尾句。"
    sel = "中间这句要改写得更专业"
    start = content.index(sel)
    end = start + len(sel)
    # Returns prefix原文 + rewrite + suffix原文 (whole paragraph echoed).
    repl = "前面是第一段完整内容。中间这段改写后更加专业清晰。后面还有结尾的收尾句。"
    out, apply_to = normalize_selection_replacement(repl, content, start, end)
    assert apply_to == "selection"
    assert out == "中间这段改写后更加专业清晰"
    # No原文 leaked either side.
    assert "前面是第一段完整内容" not in out
    assert "后面还有结尾的收尾句" not in out


def test_normalize_trims_full_text_echo() -> None:
    """Model returns the ENTIRE doc (prefix+rewrite+suffix verbatim) → middle片段."""
    content = "开头第一段是这样写的内容。中间这句要改。结尾最后一段这样收束全文。"
    sel = "中间这句要改。"
    start = content.index(sel)
    end = start + len(sel)
    repl = "开头第一段是这样写的内容。中间改好的句子在这里。结尾最后一段这样收束全文。"
    out, apply_to = normalize_selection_replacement(repl, content, start, end)
    assert apply_to == "selection"
    assert out == "中间改好的句子在这里。"


def test_normalize_end_selection_only_trims_head() -> None:
    """选区在文末（suffix 为空）→ 只剪头部回显，保留改写后的结尾。"""
    body = "这是文章的正文部分，讲了很多内容，铺陈了论点和论据，篇幅比较长。"
    ending = "最后用这句话收尾。"
    content = body + ending
    start = content.index(ending)
    end = len(content)
    # Model returns whole doc: unchanged body verbatim + rewritten ending.
    repl = body + "结尾改写成了一句更有力的收束。"
    out, apply_to = normalize_selection_replacement(repl, content, start, end)
    assert apply_to == "selection"
    assert out == "结尾改写成了一句更有力的收束。"
    assert body not in out


def test_normalize_document_fallback_on_fingerprint() -> None:
    """No >=10 overlap to trim, but fingerprint + length ⇒ whole rewrite (document)."""
    content = "开头。改这里后面的原始内容会被模型改写掉所以尾部不重叠。"
    sel = "改这里"
    start = content.index(sel)
    end = start + len(sel)
    # prefix "开头。" is <10 chars (no head trim), suffix reworded (no tail trim),
    # but replacement echoes the doc's opening fingerprint and is long.
    repl = content + "并且额外补充了大量新的段落内容使篇幅远超原文长度好几倍之多需要更多字数。" * 8
    out, apply_to = normalize_selection_replacement(repl, content, start, end)
    assert apply_to == "document"
    assert out == repl  # returned verbatim as the new whole document


def test_normalize_short_overlap_not_trimmed() -> None:
    """<10 字符的巧合公共串不应被误剪；守约改写原样保留。"""
    content = "他说的这句话。需要改进的部分在这里。剩下的原文内容。"
    sel = "需要改进的部分在这里"
    start = content.index(sel)
    end = start + len(sel)
    repl = "这里改成了另一种说法"  # incidental short overlap only
    out, apply_to = normalize_selection_replacement(repl, content, start, end)
    assert apply_to == "selection"
    assert out == repl


def test_normalize_clean_fragment_unchanged() -> None:
    """守约的净片段（无任何回显）不受影响。"""
    content = "第一段文字。\n\n这一句需要修改，保持其他内容不变。"
    sel = "这一句需要修改"
    start = content.index(sel)
    end = start + len(sel)
    repl = "这句话可以说得更自然一点"
    out, apply_to = normalize_selection_replacement(repl, content, start, end)
    assert apply_to == "selection"
    assert out == repl


def test_normalize_invalid_bounds_pass_through() -> None:
    """Out-of-range start/end → return replacement unchanged as selection."""
    content = "短文本"
    out, apply_to = normalize_selection_replacement("随便", content, 5, 99)
    assert apply_to == "selection"
    assert out == "随便"


def test_normalize_whitespace_tolerant_overlap() -> None:
    """回显的原文空白与画布不完全一致时（多/少换行）仍能识别并剪掉。

    重叠段落须 >= _MIN_OVERLAP(10) 才判回显，故用较长的段落。
    """
    prefix_para = "这是文章开头相当完整的第一段落内容铺陈。"
    suffix_para = "这是文章结尾相当完整的最后段落收束全文。"
    sel = "中间要改的这句话。"
    content = f"{prefix_para}\n\n{sel}\n\n{suffix_para}"
    start = content.index(sel)
    end = start + len(sel)
    # Model collapses the paragraph breaks into single spaces around the rewrite.
    repl = f"{prefix_para} 中间改写后的新句子在此。 {suffix_para}"
    out, apply_to = normalize_selection_replacement(repl, content, start, end)
    assert apply_to == "selection"
    assert prefix_para not in out
    assert suffix_para not in out
    assert "中间改写后的新句子在此" in out


# ---------------------------------------------------------------------------
# Endpoint integration: apply_to routing
# ---------------------------------------------------------------------------

def test_canvas_edit_trims_echoed_full_text(
    client: TestClient, authed_cookie: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: model echoes surrounding原文 → BFF returns the trimmed片段."""
    content = "前面是第一段完整内容。中间这句要改写得更专业。后面还有结尾的收尾句。"
    sel = "中间这句要改写得更专业"
    start = content.index(sel)
    end = start + len(sel)
    fake = FakeGateway(
        completion_content=(
            "<canvas-replacement>\n"
            "前面是第一段完整内容。中间这段改写后更加专业清晰。后面还有结尾的收尾句。\n"
            "</canvas-replacement>"
        )
    )
    _patch(fake, monkeypatch)

    resp = client.post(
        f"{PREFIX}/sessions/ses_abc/canvas/edit",
        cookies=authed_cookie,
        json=_body(
            content=content,
            selection={"text": sel, "start": start, "end": end},
        ),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["apply_to"] == "selection"
    assert body["replacement"] == "中间这段改写后更加专业清晰"


def test_canvas_edit_document_fallback_returns_apply_to_document(
    client: TestClient, authed_cookie: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Whole-rewrite disguised as selection → apply_to=document, replacement原样."""
    content = "开头。改这里后面的原始内容会被模型改写掉所以尾部不重叠。"
    sel = "改这里"
    start = content.index(sel)
    end = start + len(sel)
    whole = content + "并且额外补充了大量新的段落内容使篇幅远超原文长度好几倍之多需要更多字数。" * 8
    fake = FakeGateway(
        completion_content=f"<canvas-replacement>\n{whole}\n</canvas-replacement>"
    )
    _patch(fake, monkeypatch)

    resp = client.post(
        f"{PREFIX}/sessions/ses_abc/canvas/edit",
        cookies=authed_cookie,
        json=_body(
            content=content,
            selection={"text": sel, "start": start, "end": end},
        ),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["apply_to"] == "document"
    assert body["replacement"] == whole


def test_canvas_edit_document_scope_reports_apply_to_document(
    client: TestClient, authed_cookie: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """scope=document requests are naturally apply_to=document."""
    fake = FakeGateway(
        completion_content="<canvas-replacement>\n整篇改写后的新内容\n</canvas-replacement>"
    )
    _patch(fake, monkeypatch)

    resp = client.post(
        f"{PREFIX}/sessions/ses_abc/canvas/edit",
        cookies=authed_cookie,
        json=_body(scope="document", selection=None),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["apply_to"] == "document"
    assert body["replacement"] == "整篇改写后的新内容"


def test_canvas_edit_trim_to_empty_yields_502(
    client: TestClient, authed_cookie: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """If trimming removes everything (model echoed only原文) → 502 empty修订."""
    content = "前面原文很长的一段铺陈内容在这里。要改的这一句。后面原文的收尾段落。"
    sel = "要改的这一句。"
    start = content.index(sel)
    end = start + len(sel)
    # Model returns prefix+suffix原文 verbatim with nothing new in between.
    echoed = "前面原文很长的一段铺陈内容在这里。后面原文的收尾段落。"
    fake = FakeGateway(
        completion_content=f"<canvas-replacement>\n{echoed}\n</canvas-replacement>"
    )
    _patch(fake, monkeypatch)

    resp = client.post(
        f"{PREFIX}/sessions/ses_abc/canvas/edit",
        cookies=authed_cookie,
        json=_body(
            content=content,
            selection={"text": sel, "start": start, "end": end},
        ),
    )
    assert resp.status_code == 502
    assert "valid revision" in resp.json()["detail"]["user_message"]
