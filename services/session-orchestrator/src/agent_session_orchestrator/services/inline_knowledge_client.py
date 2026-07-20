"""Knowledge-ingest client for the session-consumption side.

Two consumers share this module so the ``GET /search?scope=knowledge`` call and
its 【文档名 | 文件夹路径】 provenance formatting live in exactly one place:

* ``create_run``'s **prefetch** (``fetch_knowledge_prefetch``) — top-k knowledge
  hits injected into the system prompt as the "知识库资料" block;
* the inline tool loop's **search_knowledge / list_knowledge** tools — the model
  pulls knowledge on demand mid-conversation.

Every call carries the run's **effective clearance** (``min(user, workspace)``)
in the ``X-User-Clearance`` header so knowledge-ingest enforces the visibility
gate.  Everything is **fail-open**: a retrieval outage must never block the
conversation.  The prefetch returns ``""`` on any error; the tools return a
short Chinese "service unavailable" note the model can relay, never raising.

Contract (per docs/knowledge-library-v1.md — we do NOT read the ki source):

* ``GET /search`` params ``q``, ``k``, ``scope=knowledge``; hit shape
  ``{document_id, document_name, folder_path, chunk_id, text, score, scope}``
  (``folder_path`` = ``产品/合同模板``; empty string at the root).
* ``GET /documents`` returns the documents visible at the header clearance, each
  carrying at least name / folder / sensitivity / status.  Response parsing is
  deliberately tolerant of field-name aliases and list-vs-envelope shapes.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Timeout for a single knowledge-ingest call (both search + list).  Retrieval is
# on the interactive path, so keep it short — a slow ki must not stall the reply.
_KNOWLEDGE_TIMEOUT = 5.0

# Hard cap on the document rows list_knowledge surfaces to the model, so a large
# knowledge base never floods the tool result / context.
_MAX_LIST_DOCUMENTS = 100


def _headers(tenant_id: str, user_id: str, clearance: str) -> dict[str, str]:
    """ki identity + clearance headers (never the SYSTEM sentinel for user_id)."""
    return {
        "X-Tenant-Id": tenant_id,
        "X-User-Id": user_id or "SYSTEM",
        "X-User-Clearance": clearance or "L1",
    }


def _first(d: dict[str, Any], *keys: str) -> str:
    """First non-empty string value among ``keys`` (alias-tolerant field read)."""
    for key in keys:
        val = d.get(key)
        if val is not None and str(val).strip():
            return str(val).strip()
    return ""


def format_knowledge_hit(hit: dict[str, Any]) -> str:
    """One RAG hit → ``【文档名 | 文件夹路径】\\n<text>`` (path omitted at root)."""
    text = str(hit.get("text") or "").strip()
    name = _first(hit, "document_name", "name", "title") or "未命名文档"
    folder = str(hit.get("folder_path") or "").strip()
    source = f"{name} | {folder}" if folder else name
    return f"【{source}】\n{text}"


def format_knowledge_hits_block(hits: list[dict[str, Any]]) -> str:
    """Join hits into the system-prompt "知识库资料" body (blank-line separated).

    Hits with empty text are dropped; an all-empty list yields ``""``.
    """
    blocks = [
        format_knowledge_hit(hit)
        for hit in hits
        if str(hit.get("text") or "").strip()
    ]
    return "\n\n".join(blocks)


async def _search(
    *,
    base_url: str,
    tenant_id: str,
    user_id: str,
    clearance: str,
    query: str,
    k: int,
) -> list[dict[str, Any]] | None:
    """``GET /search?scope=knowledge`` → hit list, or ``None`` on any failure.

    ``None`` is the fail-open signal (network/HTTP/parse error); callers map it
    to an empty prefetch block or a "service unavailable" tool note.  An empty
    list is a *successful* search with no hits (distinct from ``None``).
    """
    if not base_url or not query.strip():
        return []
    params: dict[str, Any] = {
        "q": query[:500],
        "k": int(k) if k else 5,
        "scope": "knowledge",
    }
    try:
        async with httpx.AsyncClient(timeout=_KNOWLEDGE_TIMEOUT) as client:
            resp = await client.get(
                f"{base_url}/search",
                params=params,
                headers=_headers(tenant_id, user_id, clearance),
            )
        if resp.status_code != 200:
            logger.debug("knowledge search returned %s (fail-open)", resp.status_code)
            return None
        hits = resp.json().get("hits") or []
        return hits if isinstance(hits, list) else []
    except Exception as exc:  # noqa: BLE001 — fail-open: retrieval never blocks chat
        logger.debug("knowledge search failed (fail-open): %s", exc)
        return None


async def fetch_knowledge_prefetch(
    *,
    base_url: str,
    tenant_id: str,
    user_id: str,
    clearance: str,
    query: str,
    k: int = 5,
) -> str:
    """Prefetch top-k knowledge hits → formatted "知识库资料" block, or ``""``.

    Failure-open: any error (unreachable, non-200, unparsable) → ``""`` so the
    run proceeds without knowledge context, exactly like the memory prefetch.
    """
    hits = await _search(
        base_url=base_url, tenant_id=tenant_id, user_id=user_id,
        clearance=clearance, query=query, k=k,
    )
    if not hits:
        return ""
    return format_knowledge_hits_block(hits)


async def search_knowledge(
    *,
    base_url: str,
    tenant_id: str,
    user_id: str,
    clearance: str,
    query: str,
    k: int = 5,
) -> str:
    """``search_knowledge`` tool body → model-facing result text (never raises).

    * unreachable / error  → short "service unavailable" note (fail-open);
    * no hits              → "未检索到" note;
    * hits                → a count header + each 【文档名 | 文件夹路径】 block.
    """
    hits = await _search(
        base_url=base_url, tenant_id=tenant_id, user_id=user_id,
        clearance=clearance, query=query, k=k,
    )
    if hits is None:
        return "（知识库检索服务暂时不可用，本次未能查询知识库。）"
    block = format_knowledge_hits_block(hits)
    if not block:
        return f"未在租户知识库中检索到与「{query.strip()}」相关的资料。"
    count = block.count("【")
    return f"在租户知识库中检索到 {count} 条相关资料：\n\n{block}"


def _format_document_row(doc: dict[str, Any]) -> str:
    """One /documents row → ``- 名称（路径：…；密级：…；状态：…）``."""
    name = _first(doc, "document_name", "name", "filename", "title") or "未命名文档"
    folder = _first(doc, "folder_path", "folder_id") or "根目录"
    sensitivity = _first(doc, "sensitivity", "sensitivity_level") or "L1"
    status = _first(doc, "status", "state", "ingest_status") or "未知"
    return f"- {name}（路径：{folder}；密级：{sensitivity}；状态：{status}）"


def _extract_document_rows(payload: Any) -> list[dict[str, Any]]:
    """Pull the document list out of a bare-list or enveloped /documents body."""
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        rows = []
        for field in ("items", "documents", "data", "results"):
            candidate = payload.get(field)
            if isinstance(candidate, list):
                rows = candidate
                break
    else:
        rows = []
    return [r for r in rows if isinstance(r, dict)]


async def list_knowledge(
    *,
    base_url: str,
    tenant_id: str,
    user_id: str,
    clearance: str,
) -> str:
    """``list_knowledge`` tool body → model-facing document listing (never raises).

    ``GET /documents`` with the clearance header; ki returns only the documents
    visible at that clearance.  Fail-open: any error → a short unavailable note.
    """
    if not base_url:
        return "（知识库服务未配置，无法列出文档。）"
    try:
        async with httpx.AsyncClient(timeout=_KNOWLEDGE_TIMEOUT) as client:
            resp = await client.get(
                f"{base_url}/documents",
                headers=_headers(tenant_id, user_id, clearance),
            )
        if resp.status_code != 200:
            logger.debug("knowledge list returned %s (fail-open)", resp.status_code)
            return "（知识库检索服务暂时不可用，本次未能列出文档。）"
        rows = _extract_document_rows(resp.json())
    except Exception as exc:  # noqa: BLE001 — fail-open: retrieval never blocks chat
        logger.debug("knowledge list failed (fail-open): %s", exc)
        return "（知识库检索服务暂时不可用，本次未能列出文档。）"

    if not rows:
        return "当前你在该密级下没有可见的知识库文档（知识库可能为空）。"
    shown = rows[:_MAX_LIST_DOCUMENTS]
    lines = [f"知识库中你当前可见的文档（共 {len(rows)} 个）："]
    lines.extend(_format_document_row(doc) for doc in shown)
    if len(rows) > _MAX_LIST_DOCUMENTS:
        lines.append(f"…（仅显示前 {_MAX_LIST_DOCUMENTS} 个）")
    return "\n".join(lines)
