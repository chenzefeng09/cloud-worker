"""Knowledge-ingest client for the session-consumption side.

``create_run`` prefetches the top-k tenant-knowledge hits for the task
(``fetch_knowledge_prefetch``, ``GET /search?scope=knowledge``) and folds them,
with ``[Document | folder/path]`` provenance, into the worker's system prompt.

Every call carries the run's **effective clearance** (``min(user, workspace)``)
in the ``X-User-Clearance`` header so knowledge-ingest enforces the visibility
gate.  Everything is **fail-open**: a retrieval outage must never block the
conversation.  The prefetch returns ``""`` on any error.

Contract (per docs/knowledge-library-v1.md — we do NOT read the ki source):

* ``GET /search`` params ``q``, ``k``, ``scope=knowledge``; hit shape
  ``{document_id, document_name, folder_path, chunk_id, text, score, scope}``
  (``folder_path`` = ``Products/Contract templates``; empty string at the root).
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
    """One RAG hit → ``[Document | folder/path]\\n<text>`` (path omitted at root)."""
    text = str(hit.get("text") or "").strip()
    name = _first(hit, "document_name", "name", "title") or "Untitled document"
    folder = str(hit.get("folder_path") or "").strip()
    source = f"{name} | {folder}" if folder else name
    return f"[{source}]\n{text}"


def format_knowledge_hits_block(hits: list[dict[str, Any]]) -> str:
    """Join hits into the system-prompt "Knowledge base excerpts" body (blank-line separated).

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
    """Prefetch top-k knowledge hits → formatted "Knowledge base excerpts" block, or ``""``.

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
