"""Inline-path knowledge-base tool spec — the ``search_knowledge`` /
``list_knowledge`` OpenAI contracts plus the knowledge system-prompt section.

Background
----------
The tenant **knowledge base** is a租户级资源库: documents uploaded and curated by
tenant admins, retrievable (under a clearance gate) by every member.  It is a
*separate* source from the two file channels the inline path already injects:

* **会话附件** — files the user uploaded into THIS session (transient).
* **工作区基础文件** — the workspace's base files (``workspace_files``).

On the **inline** path there is no sandbox and no ``Read`` tool, so — mirroring
the ``canvas_tool_spec`` / ``skill_tool_spec`` pattern — this module is the single
source of truth for the two knowledge tools the executor advertises to the
gateway and dispatches in-process against knowledge-ingest:

* ``search_knowledge(query, k=5)`` → ki ``GET /search?scope=knowledge`` (RAG hits
  with 【文档名 | 文件夹路径】 provenance);
* ``list_knowledge()`` → ki ``GET /documents`` (the documents visible at the
  caller's effective clearance).

Both carry the run's **effective clearance** (``min(user, workspace)``) in the
``X-User-Clearance`` header so the visibility gate is enforced ki-side.

The tool descriptions are deliberately directive (Chinese): weak-instruction
models — the deepseek family this platform targets — only call a tool reliably
when the description spells out *what the knowledge base is* and *when to call*.
"""

from __future__ import annotations

from typing import Any

# ── Tool names ───────────────────────────────────────────────────────────────
SEARCH_KNOWLEDGE_TOOL_NAME = "search_knowledge"
LIST_KNOWLEDGE_TOOL_NAME = "list_knowledge"

# Default hit count for search_knowledge (matches the create_run prefetch k).
DEFAULT_KNOWLEDGE_K = 5


# ── OpenAI tool schemas ──────────────────────────────────────────────────────
SEARCH_KNOWLEDGE_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": SEARCH_KNOWLEDGE_TOOL_NAME,
        "description": (
            "在【租户知识库】中检索资料并返回最相关的若干条内容。"
            "租户知识库是由管理员上传维护、面向全体成员的资料库"
            "（如合同模板、产品文档、规章制度、技术资料等），"
            "它独立于「当前会话上传的附件」和「工作区基础文件」——"
            "即使工作区没有基础文件，知识库里也可能有资料。\n"
            "何时调用：当用户的问题可能需要引用组织内部沉淀的资料、规范、模板或权威事实，"
            "或你并不确定答案而这些信息可能已存在于知识库中时，"
            "**必须先调用本工具检索**，再依据命中的资料作答。"
            "返回的每条命中都带有【文档名 | 文件夹路径】出处，请据此回答并注明来源。"
            "严禁在未调用本工具的情况下声称查过知识库，也不要凭空编造知识库内容。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "检索用的关键词或自然语言问题，用中文或原文描述你要在知识库里找的内容。"
                    ),
                },
                "k": {
                    "type": "integer",
                    "description": "返回的最大命中条数，默认 5。",
                    "default": DEFAULT_KNOWLEDGE_K,
                },
            },
            "required": ["query"],
        },
    },
}

LIST_KNOWLEDGE_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": LIST_KNOWLEDGE_TOOL_NAME,
        "description": (
            "列出【租户知识库】中你在当前密级下可见的文档清单，"
            "每个文档包含名称、所在文件夹路径、密级与状态。"
            "何时调用：当用户想知道知识库里有哪些资料，"
            "或你需要先浏览有哪些文档、再决定用 search_knowledge 检索什么时调用。"
            "本工具无需任何参数。"
        ),
        "parameters": {"type": "object", "properties": {}},
    },
}


# ── System-prompt section ────────────────────────────────────────────────────
# Injected whenever knowledge-ingest is reachable (the two tools are advertised).
# Kept EXPLICITLY separate from ``workspace_empty_notice`` (base files): a model
# that hears "工作区没有基础文件" must not conclude "所以没有知识库".
KNOWLEDGE_SYSTEM_PROMPT = (
    "\n\n## 租户知识库（search_knowledge / list_knowledge）\n"
    "本租户维护着一个【知识库】：由管理员上传维护、面向全体成员的资料库"
    "（如合同模板、产品文档、规章制度、技术资料等）。"
    "它与「当前会话上传的附件」「工作区基础文件」是三个相互独立的来源——"
    "即使工作区没有配置任何基础文件，知识库依然可能有资料，"
    "**不要把「没有基础文件」当成「没有知识库」**。\n"
    "当用户的问题可能依赖组织内部沉淀的资料、规范、模板或权威事实，"
    "而你并不确定答案、或答案需要引用内部资料时，"
    "**先调用 search_knowledge 检索**，再根据命中的资料作答，并注明出处（文档名/文件夹路径）。"
    "想先了解知识库里有哪些文档时，可调用 list_knowledge 列出你当前可见的文档清单。\n"
    "如实说明检索结果：检索到就依据资料回答并给出出处；"
    "确实没有相关资料（或知识库为空）时，就如实告知未在知识库中找到，"
    "不要编造知识库内容，也不要谎称调用过工具。"
)
