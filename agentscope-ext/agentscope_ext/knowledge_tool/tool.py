"""``search_knowledge`` tool - lets the in-sandbox agent query the tenant KB.

Mirrors the **inline** path's ``search_knowledge`` contract
(``session-orchestrator/.../inline_knowledge_client.py``) but runs INSIDE the
sandbox worker.  The inline path calls knowledge-ingest in-process from the
orchestrator; this tool calls it over HTTP from the sandbox, using the three
identity headers the scheduler injected into the env:

* ``X-Tenant-Id``   <- ``FOUNDATION_TENANT_ID``
* ``X-User-Id``     <- ``FOUNDATION_USER_ID``
* ``X-User-Clearance`` <- ``FOUNDATION_USER_CLEARANCE`` (the run's effective
  clearance, ``min(user, workspace)``)

ki needs NO bearer / virtual key - just those three headers.  In broker egress
mode ``KNOWLEDGE_INGEST_URL`` points at the tool-broker ``/knowledge/v1``
façade; the worker additionally sends ``X-Tool-Broker-Lease`` (read from
``TOOL_BROKER_LEASE``), and the broker re-injects all three identity headers
from the signed lease - so a compromised sandbox cannot inflate its clearance
to read restricted documents.

The tool is **fail-open** by design (retrieval must never block the reply):
unreachable / non-200 -> a short "service unavailable" note the model can
relay; no hits -> a "未检索到" note; hits -> a count header + each
``【文档名 | 文件夹路径】\\n<text>`` block.  It NEVER raises.

AgentScope 2.x tool contract (same as ``web_fetch.tool.WebFetchTool``): a
``ToolBase`` subclass whose ``__call__`` returns a ``ToolChunk``.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# AgentScope 2.x base-class resolution (mirror web_fetch.tool).
# ---------------------------------------------------------------------------
try:  # pragma: no cover - exercised in the sandbox image, stubbed in tests
    from agentscope.tool import ToolBase as _ToolBase
    from agentscope.tool._response import ToolChunk as _ToolChunk
    from agentscope.message import TextBlock as _TextBlock, ToolResultState as _ToolResultState
    from agentscope.permission import (
        PermissionBehavior as _PermissionBehavior,
        PermissionContext as _PermissionContext,
        PermissionDecision as _PermissionDecision,
    )

    _AGENTSCOPE_TOOLS = True
except Exception:  # noqa: BLE001 - keep importable without agentscope
    _ToolBase = object  # type: ignore[assignment,misc]
    _ToolChunk = None  # type: ignore[assignment]
    _TextBlock = None  # type: ignore[assignment]
    _ToolResultState = None  # type: ignore[assignment]
    _PermissionBehavior = None  # type: ignore[assignment]
    _PermissionContext = Any  # type: ignore[assignment]
    _PermissionDecision = None  # type: ignore[assignment]
    _AGENTSCOPE_TOOLS = False


# Timeout for a single ki /search call.  Retrieval is on the interactive path,
# so a slow ki must not stall the reply (matches inline_knowledge_client).
_KNOWLEDGE_TIMEOUT = 5.0

# Default hit count (matches inline_knowledge_client.DEFAULT_KNOWLEDGE_K).
_DEFAULT_K = 5


def _format_hit(hit: dict[str, Any]) -> str:
    """One RAG hit -> ``【文档名 | 文件夹路径】\\n<text>`` (path omitted at root).

    Alias-tolerant field read, matching ``inline_knowledge_client.format_knowledge_hit``.
    """
    text = str(hit.get("text") or "").strip()
    name = ""
    for key in ("document_name", "name", "title"):
        val = hit.get(key)
        if val is not None and str(val).strip():
            name = str(val).strip()
            break
    name = name or "未命名文档"
    folder = str(hit.get("folder_path") or "").strip()
    source = f"{name} | {folder}" if folder else name
    return f"【{source}】\n{text}"


class SearchKnowledgeTool(_ToolBase):
    """Search the tenant knowledge base and return the top-k RAG hits.

    在【租户知识库】中检索资料并返回最相关的若干条内容。租户知识库是由管理员
    上传维护、面向全体成员的资料库（如合同模板、产品文档、规章制度、技术资料等），
    它独立于「当前会话上传的附件」和「工作区基础文件」——即使工作区没有基础文件，
    知识库里也可能有资料。

    何时调用：当用户的问题可能需要引用组织内部沉淀的资料、规范、模板或权威事实，
    或你并不确定答案而这些信息可能已存在于知识库中时，**必须先调用本工具检索**，
    再依据命中的资料作答。返回的每条命中都带有【文档名 | 文件夹路径】出处，请据此
    回答并注明来源。严禁在未调用本工具的情况下声称查过知识库，也不要凭空编造
    知识库内容。

    Args:
        query: 检索用的关键词或自然语言问题，用中文或原文描述要在知识库里找的内容。
        k: 返回的最大命中条数，默认 5。

    Returns:
        A plain-text string: a count header + each 【文档名 | 文件夹路径】 block on
        success; a short "service unavailable" / "未检索到" note otherwise.
    """

    name = "search_knowledge"

    description: str = (
        "在【租户知识库】中检索资料并返回最相关的若干条内容。"
        "租户知识库是由管理员上传维护、面向全体成员的资料库"
        "（如合同模板、产品文档、规章制度、技术资料等），"
        "它独立于「当前会话上传的附件」和「工作区基础文件」——"
        "即使工作区没有基础文件，知识库里也可能有资料。"
        "何时调用：当用户的问题可能需要引用组织内部沉淀的资料、规范、模板或权威事实，"
        "或你并不确定答案而这些信息可能已存在于知识库中时，**必须先调用本工具检索**，"
        "再依据命中的资料作答。返回的每条命中都带有【文档名 | 文件夹路径】出处，"
        "请据此回答并注明来源。严禁在未调用本工具的情况下声称查过知识库，"
        "也不要凭空编造知识库内容。"
    )

    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "检索用的关键词或自然语言问题，用中文或原文描述你要在"
                    "知识库里找的内容。"
                ),
            },
            "k": {
                "type": "integer",
                "description": "返回的最大命中条数，默认 5。",
                "default": _DEFAULT_K,
            },
        },
        "required": ["query"],
    }

    is_mcp: bool = False
    is_read_only: bool = True
    is_concurrency_safe: bool = True
    is_external_tool: bool = False
    is_state_injected: bool = False

    def __init__(
        self,
        base_url: str,
        *,
        tenant_id: str = "",
        user_id: str = "",
        clearance: str = "",
        broker_lease: str = "",
    ) -> None:
        # Strip trailing slash so we can safely append /search.
        self._base_url = base_url.rstrip("/")
        self._tenant_id = tenant_id
        self._user_id = user_id
        self._clearance = clearance
        self._broker_lease = broker_lease

    async def check_permissions(
        self,
        tool_input: dict[str, Any],
        context: Any,
    ) -> Any:
        """search_knowledge is read-only retrieval through the policy-enforcing
        ki clearance gate (or the broker façade in broker mode), so let the
        engine continue with its normal rule matching."""
        return _PermissionDecision(
            behavior=_PermissionBehavior.PASSTHROUGH,
            message="search_knowledge is read-only retrieval.",
        )

    def __call__(self, query: str, k: Optional[int] = _DEFAULT_K) -> Any:
        """Search the tenant KB and return an AgentScope ``ToolChunk``."""
        text = self._search(query, k)
        if _AGENTSCOPE_TOOLS and _ToolChunk is not None:
            return _ToolChunk(
                content=[_TextBlock(type="text", text=text)],
                state=_ToolResultState.SUCCESS,
                is_last=True,
            )
        return text

    def _search(self, query: str, k: Optional[int]) -> str:
        """GET {base}/search?scope=knowledge -> model-facing result text.

        Fail-open: never raises.  Mirrors ``inline_knowledge_client.search_knowledge``.
        """
        import httpx

        q = (query or "").strip()
        if not q:
            return "（检索词为空，未查询知识库。）"

        params: dict[str, Any] = {
            "q": q[:500],
            "k": int(k) if k else _DEFAULT_K,
            "scope": "knowledge",
        }
        headers: dict[str, str] = {}
        if self._tenant_id:
            headers["X-Tenant-Id"] = self._tenant_id
        # ``user_id or "SYSTEM"`` mirrors inline_knowledge_client._headers: ki
        # treats the SYSTEM sentinel as a service caller.
        headers["X-User-Id"] = self._user_id or "SYSTEM"
        # Missing X-User-Clearance makes ki fail-closed to L1; send the run's
        # effective clearance (empty string -> ki treats as L1, matching inline).
        headers["X-User-Clearance"] = self._clearance or "L1"
        if self._broker_lease:
            headers["X-Tool-Broker-Lease"] = self._broker_lease

        endpoint = f"{self._base_url}/search"
        try:
            resp = httpx.get(endpoint, params=params, headers=headers, timeout=_KNOWLEDGE_TIMEOUT)
        except httpx.TimeoutException:
            return "（知识库检索服务暂时不可用，本次未能查询知识库。）"
        except httpx.ConnectError as exc:
            _log.debug("knowledge search unreachable (fail-open): %s", exc)
            return "（知识库检索服务暂时不可用，本次未能查询知识库。）"
        except Exception as exc:  # noqa: BLE001 - fail-open: retrieval never blocks chat
            _log.debug("knowledge search failed (fail-open): %s", exc)
            return "（知识库检索服务暂时不可用，本次未能查询知识库。）"

        if resp.status_code != 200:
            _log.debug("knowledge search returned %s (fail-open)", resp.status_code)
            return "（知识库检索服务暂时不可用，本次未能查询知识库。）"

        try:
            hits = resp.json().get("hits") or []
        except Exception as exc:  # noqa: BLE001 - fail-open
            _log.debug("knowledge search parse failed (fail-open): %s", exc)
            return "（知识库检索服务暂时不可用，本次未能查询知识库。）"
        if not isinstance(hits, list):
            hits = []

        blocks = [_format_hit(h) for h in hits if isinstance(h, dict)]
        blocks = [b for b in blocks if b.split("】\n", 1)[-1].strip()]
        if not blocks:
            return f"未在租户知识库中检索到与「{q}」相关的资料。"
        count = sum(1 for b in blocks if b.startswith("【"))
        return f"在租户知识库中检索到 {count} 条相关资料：\n\n" + "\n\n".join(blocks)
