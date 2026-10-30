"""Unit coverage for the knowledge-ingest client (session-consumption side).

Exercises the ``POST /search`` (``scope=knowledge``) contract from
docs/knowledge-library-v1.md: the effective-clearance header, scope field, the
``[Document | folder/path]`` provenance formatting, and fail-open behaviour on every
error path (a retrieval outage must never block the conversation).

Style mirrors the other service unit tests: a fake ``httpx.AsyncClient`` that
records url/params-or-body/headers and returns canned JSON.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

import httpx

from agent_session_orchestrator.services import knowledge_client as kc


class _FakeResp:
    def __init__(self, status_code: int, payload: Any) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> Any:
        return self._payload


class _FakeKiClient:
    """Records every call (url, params or JSON body, headers) and returns configured bodies."""

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
        if url.endswith("/documents"):
            return _FakeResp(
                self.status, self.docs_payload if self.docs_payload is not None else []
            )
        raise AssertionError(f"unexpected GET {url}")

    async def post(  # type: ignore[no-untyped-def]
        self, url: str, json: dict | None = None, headers: dict | None = None, **kw: Any,
    ):
        self.calls.append((url, json or {}, headers or {}))
        if self.raise_get:
            raise httpx.ConnectError("ki down")
        if url.endswith("/search"):
            return _FakeResp(self.status, {"hits": self.search_hits or []})
        raise AssertionError(f"unexpected POST {url}")


def _await(coro):  # type: ignore[no-untyped-def]
    return asyncio.get_event_loop().run_until_complete(coro)


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
    assert block == "[手册 | 技术]\n步骤一"

    fake_err = _FakeKiClient(raise_get=True)
    with patch.object(httpx, "AsyncClient", return_value=fake_err):
        empty = _await(
            kc.fetch_knowledge_prefetch(
                base_url="http://ki/api/knowledge/v1",
                tenant_id="tnt", user_id="usr_1", clearance="L4", query="怎么做",
            )
        )
    assert empty == ""
