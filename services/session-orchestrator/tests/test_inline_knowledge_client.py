"""Unit coverage for the knowledge-ingest client (session-consumption side).

Exercises the ``GET /search?scope=knowledge`` + ``GET /documents`` contract from
docs/knowledge-library-v1.md: the effective-clearance header, scope param, the
【文档名 | 文件夹路径】 provenance formatting, and fail-open behaviour on every
error path (a retrieval outage must never block the conversation).

Style mirrors the other service unit tests: a fake ``httpx.AsyncClient`` that
records url/params/headers and returns canned JSON.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

import httpx

from agent_session_orchestrator.services import inline_knowledge_client as kc


class _FakeResp:
    def __init__(self, status_code: int, payload: Any) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> Any:
        return self._payload


class _FakeKiClient:
    """Records every GET (url, params, headers) and returns configured bodies."""

    def __init__(
        self,
        *,
        search_hits: list | None = None,
        docs_payload: Any = None,
        status: int = 200,
        raise_get: bool = False,
    ) -> None:
        self.search_hits = search_hits
        self.docs_payload = docs_payload
        self.status = status
        self.raise_get = raise_get
        self.calls: list[tuple[str, dict, dict]] = []

    async def __aenter__(self) -> _FakeKiClient:
        return self

    async def __aexit__(self, *a: Any) -> None:
        return None

    async def get(self, url: str, params: dict | None = None, headers: dict | None = None, **kw: Any):  # type: ignore[no-untyped-def]
        self.calls.append((url, params or {}, headers or {}))
        if self.raise_get:
            raise httpx.ConnectError("ki down")
        if url.endswith("/search"):
            return _FakeResp(self.status, {"hits": self.search_hits or []})
        if url.endswith("/documents"):
            return _FakeResp(
                self.status, self.docs_payload if self.docs_payload is not None else []
            )
        raise AssertionError(f"unexpected GET {url}")


def _await(coro):  # type: ignore[no-untyped-def]
    return asyncio.get_event_loop().run_until_complete(coro)


# ── search_knowledge ─────────────────────────────────────────────────────────


def test_search_sends_scope_and_clearance_and_formats_provenance() -> None:
    fake = _FakeKiClient(
        search_hits=[
            {"document_name": "合同模板", "folder_path": "产品/合同", "text": "第一条…"},
            {"document_name": "报销规定", "folder_path": "", "text": "差旅报销…"},
        ]
    )
    with patch.object(httpx, "AsyncClient", return_value=fake):
        result = _await(
            kc.search_knowledge(
                base_url="http://ki/api/knowledge/v1",
                tenant_id="tnt", user_id="usr_1", clearance="L2",
                query="报销", k=5,
            )
        )
    url, params, headers = fake.calls[0]
    assert url.endswith("/search")
    assert params["scope"] == "knowledge" and params["q"] == "报销" and params["k"] == 5
    assert headers["X-User-Clearance"] == "L2"
    assert headers["X-User-Id"] == "usr_1"
    # Provenance: folder path shown when present, omitted at the root.
    assert "【合同模板 | 产品/合同】" in result
    assert "【报销规定】" in result
    assert "检索到 2 条" in result


def test_search_no_hits_returns_not_found_note() -> None:
    fake = _FakeKiClient(search_hits=[])
    with patch.object(httpx, "AsyncClient", return_value=fake):
        result = _await(
            kc.search_knowledge(
                base_url="http://ki/api/knowledge/v1",
                tenant_id="tnt", user_id="usr_1", clearance="L3", query="不存在",
            )
        )
    assert "未在租户知识库中检索到" in result


def test_search_fail_open_on_network_error() -> None:
    fake = _FakeKiClient(raise_get=True)
    with patch.object(httpx, "AsyncClient", return_value=fake):
        result = _await(
            kc.search_knowledge(
                base_url="http://ki/api/knowledge/v1",
                tenant_id="tnt", user_id="usr_1", clearance="L1", query="x",
            )
        )
    assert "暂时不可用" in result


def test_search_fail_open_on_non_200() -> None:
    fake = _FakeKiClient(search_hits=[], status=500)
    with patch.object(httpx, "AsyncClient", return_value=fake):
        result = _await(
            kc.search_knowledge(
                base_url="http://ki/api/knowledge/v1",
                tenant_id="tnt", user_id="usr_1", clearance="L1", query="x",
            )
        )
    assert "暂时不可用" in result


# ── fetch_knowledge_prefetch ─────────────────────────────────────────────────


def test_prefetch_returns_block_and_empty_on_error() -> None:
    fake_ok = _FakeKiClient(
        search_hits=[{"document_name": "手册", "folder_path": "技术", "text": "步骤一"}]
    )
    with patch.object(httpx, "AsyncClient", return_value=fake_ok):
        block = _await(
            kc.fetch_knowledge_prefetch(
                base_url="http://ki/api/knowledge/v1",
                tenant_id="tnt", user_id="usr_1", clearance="L4", query="怎么做",
            )
        )
    assert block == "【手册 | 技术】\n步骤一"

    fake_err = _FakeKiClient(raise_get=True)
    with patch.object(httpx, "AsyncClient", return_value=fake_err):
        empty = _await(
            kc.fetch_knowledge_prefetch(
                base_url="http://ki/api/knowledge/v1",
                tenant_id="tnt", user_id="usr_1", clearance="L4", query="怎么做",
            )
        )
    assert empty == ""


# ── list_knowledge ───────────────────────────────────────────────────────────


def test_list_formats_rows_with_all_fields() -> None:
    fake = _FakeKiClient(
        docs_payload=[
            {"name": "合同模板", "folder_path": "产品/合同", "sensitivity": "L2", "status": "ready"},
            {"document_name": "根目录文档", "sensitivity_level": "L1", "state": "processing"},
        ]
    )
    with patch.object(httpx, "AsyncClient", return_value=fake):
        result = _await(
            kc.list_knowledge(
                base_url="http://ki/api/knowledge/v1",
                tenant_id="tnt", user_id="usr_1", clearance="L2",
            )
        )
    url, _params, headers = fake.calls[0]
    assert url.endswith("/documents")
    assert headers["X-User-Clearance"] == "L2"
    assert "共 2 个" in result
    assert "合同模板（路径：产品/合同；密级：L2；状态：ready）" in result
    # Root document falls back to 根目录; status alias 'state' is read.
    assert "根目录文档（路径：根目录；密级：L1；状态：processing）" in result


def test_list_accepts_envelope_shape() -> None:
    fake = _FakeKiClient(docs_payload={"items": [{"name": "X", "sensitivity": "L1"}]})
    with patch.object(httpx, "AsyncClient", return_value=fake):
        result = _await(
            kc.list_knowledge(
                base_url="http://ki/api/knowledge/v1",
                tenant_id="tnt", user_id="usr_1", clearance="L1",
            )
        )
    assert "共 1 个" in result and "X" in result


def test_list_empty_and_fail_open() -> None:
    fake_empty = _FakeKiClient(docs_payload=[])
    with patch.object(httpx, "AsyncClient", return_value=fake_empty):
        result = _await(
            kc.list_knowledge(
                base_url="http://ki/api/knowledge/v1",
                tenant_id="tnt", user_id="usr_1", clearance="L1",
            )
        )
    assert "没有可见的知识库文档" in result

    fake_err = _FakeKiClient(raise_get=True)
    with patch.object(httpx, "AsyncClient", return_value=fake_err):
        result = _await(
            kc.list_knowledge(
                base_url="http://ki/api/knowledge/v1",
                tenant_id="tnt", user_id="usr_1", clearance="L1",
            )
        )
    assert "暂时不可用" in result
