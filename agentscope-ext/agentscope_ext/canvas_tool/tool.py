"""``create_canvas`` tool — lets the in-sandbox agent promote a long-form
deliverable into a first-class canvas document (canvas v2, Phase B).

The tool does NOT talk to the orchestrator over HTTP (there is no worker→
orchestrator callback path — architecture fact).  Instead it writes the canvas
payload to the run's Redis hot-state via :class:`RunStateClient.push_canvas`
(key ``agent:run:{run_id}:canvas``, a JSON list so multiple calls in one run
accumulate).  The orchestrator reads that list on both the streaming and the
run-completion reconcile paths and materializes each not-yet-seen entry into a
``canvas_document`` + ``canvas_version`` (design §6.2/6.3).

A ``/workspace/canvas.json`` mirror is written too as a durable local record
(diagnostic / belt-and-suspenders); the authoritative delivery channel is
Redis.  Both writes are failure-open: a canvas write must never crash the run —
the reply text is always delivered via the normal message path regardless.

AgentScope 2.x tool contract
-----------------------------
Same as ``WebFetchTool`` / ``AskUserTool``: a ``ToolBase`` subclass whose
instance is appended to ``toolkit.tool_groups[0].tools``.  AgentScope reads
``name`` / ``description`` / ``input_schema`` from the instance to build the
JSON schema exposed to the LLM, and dispatches via ``__call__``.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional

_log = logging.getLogger(__name__)

# Same workspace root the runtime worker uses; kept local so this module does
# not import the (heavier) worker entrypoint at import time.
_WORKSPACE = Path("/workspace")
_CANVAS_MIRROR = _WORKSPACE / "canvas.json"


# ---------------------------------------------------------------------------
# AgentScope 2.x base-class resolution (mirrors web_fetch/tool.py): resolve the
# real ToolBase in the sandbox image, degrade to plain ``object`` in unit tests
# / lint so the module stays importable everywhere.
# ---------------------------------------------------------------------------
try:  # pragma: no cover - exercised in the sandbox image, stubbed in tests
    from agentscope.message import (
        TextBlock as _TextBlock,
        ToolResultState as _ToolResultState,
    )
    from agentscope.permission import (
        PermissionBehavior as _PermissionBehavior,
        PermissionDecision as _PermissionDecision,
    )
    from agentscope.tool import ToolBase as _ToolBase
    from agentscope.tool._response import ToolChunk as _ToolChunk

    _AGENTSCOPE_TOOLS = True
except Exception:  # noqa: BLE001 - keep importable without agentscope
    _ToolBase = object  # type: ignore[assignment,misc]
    _ToolChunk = None  # type: ignore[assignment]
    _TextBlock = None  # type: ignore[assignment]
    _ToolResultState = None  # type: ignore[assignment]
    _PermissionBehavior = None  # type: ignore[assignment]
    _PermissionDecision = None  # type: ignore[assignment]
    _AGENTSCOPE_TOOLS = False


class CreateCanvasTool(_ToolBase):
    """Promote a substantial written deliverable into an editable canvas.

    Call this tool ONLY for long-form deliverables the user will likely want to
    keep iterating on as a document — an article, report, brief, proposal,
    email draft, social post, or code note of real length.  The tool captures
    the finished content as a versioned canvas the user can then edit block by
    block.

    Do NOT call this tool for ordinary conversation: short answers,
    explanations, quick lists, code snippets shown for reading, or anything the
    user did not ask to be produced as a standalone document.  When in doubt,
    do NOT create a canvas — just reply normally in the message body.

    After you call this tool, keep your natural-language reply to ONE short
    sentence telling the user the document is in the canvas (e.g. "已为你创建
    画布《…》，可在右侧查看和编辑。").  Do NOT repeat the full content in the
    reply — it already lives in the canvas.

    Args:
        kind: The document category.  One of ``article``, ``brief``, ``email``,
            ``report``, ``proposal``, ``social_post``, ``code_note``.  Defaults
            to ``article`` if unsure.
        title: A concise human-readable title for the document (used as the
            canvas heading and in listings).
        content_markdown: The full document body in Markdown.  Use ``#`` /
            ``##`` headings and blank-line-separated paragraphs — the server
            splits this into editable blocks.

    Returns:
        A short confirmation string that names the created canvas by title.
    """

    name = "create_canvas"

    description: str = (
        "Promote a long-form written deliverable (article, report, brief, "
        "proposal, email draft, social post, code note) into a first-class, "
        "editable canvas document. Use ONLY for substantial documents the user "
        "will want to keep iterating on — never for ordinary short answers, "
        "explanations, or code snippets shown for reading. After calling this, "
        "reply with just one short sentence saying the document is in the "
        "canvas; do not repeat the content in the message body."
    )

    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "kind": {
                "type": "string",
                "description": (
                    "Document category. One of: article, brief, email, report, "
                    "proposal, social_post, code_note. Defaults to 'article'."
                ),
                "enum": [
                    "article",
                    "brief",
                    "email",
                    "report",
                    "proposal",
                    "social_post",
                    "code_note",
                ],
                "default": "article",
            },
            "title": {
                "type": "string",
                "description": "A concise human-readable title for the document.",
            },
            "content_markdown": {
                "type": "string",
                "description": (
                    "The full document body in Markdown. Use '#'/'##' headings "
                    "and blank-line-separated paragraphs; the server splits this "
                    "into editable blocks."
                ),
            },
        },
        "required": ["title", "content_markdown"],
    }

    is_mcp: bool = False
    is_read_only: bool = False
    is_concurrency_safe: bool = False
    is_external_tool: bool = False
    is_state_injected: bool = False

    def __init__(self, state_client: Any) -> None:
        # ``state_client`` is a RunStateClient (failure-open) — the tool never
        # touches Redis directly, so it works identically in direct-Redis and
        # broker-egress modes.
        self._state_client = state_client

    async def check_permissions(
        self,
        tool_input: dict[str, Any],
        context: Any,
    ) -> Any:
        """create_canvas only writes hot-state (no external side effects), so
        let the engine continue with its normal rule matching (BYPASS/ALLOW).
        """
        return _PermissionDecision(
            behavior=_PermissionBehavior.PASSTHROUGH,
            message="create_canvas writes run hot-state only.",
        )

    def __call__(
        self,
        title: str,
        content_markdown: str,
        kind: Optional[str] = "article",
    ) -> Any:
        text = self._create(title=title, content_markdown=content_markdown, kind=kind)
        if _AGENTSCOPE_TOOLS and _ToolChunk is not None:
            return _ToolChunk(
                content=[_TextBlock(type="text", text=text)],
                state=_ToolResultState.SUCCESS,
                is_last=True,
            )
        return text

    def _create(
        self,
        *,
        title: str,
        content_markdown: str,
        kind: Optional[str],
    ) -> str:
        payload = {
            "kind": (kind or "article"),
            "title": title or "未命名画布",
            "content_markdown": content_markdown or "",
        }

        # Primary channel: Redis hot-state (read by the orchestrator).
        seq = 0
        try:
            seq = int(self._state_client.push_canvas(payload))
        except Exception as exc:  # noqa: BLE001 - never break the run
            _log.warning("create_canvas: push_canvas failed: %r", exc)

        # Durable local mirror (diagnostic fallback).  Non-destructive: appends
        # to a dedicated file that no other code path rewrites.
        self._mirror_to_workspace(payload)

        _log.info(
            "create_canvas: recorded canvas payload (title=%r, kind=%s, seq=%s, "
            "chars=%d)",
            payload["title"],
            payload["kind"],
            seq,
            len(payload["content_markdown"]),
        )
        return (
            f"已创建画布《{payload['title']}》。内容已放入画布，"
            "请只用一句话告知用户可在画布中查看和编辑，不要在正文里重复全文。"
        )

    def _mirror_to_workspace(self, payload: dict) -> None:
        """Append the payload to ``/workspace/canvas.json`` (failure-open)."""
        try:
            existing: list = []
            if _CANVAS_MIRROR.is_file():
                try:
                    existing = json.loads(_CANVAS_MIRROR.read_text(encoding="utf-8"))
                    if not isinstance(existing, list):
                        existing = []
                except Exception:  # noqa: BLE001
                    existing = []
            existing.append(payload)
            _CANVAS_MIRROR.write_text(
                json.dumps(existing, ensure_ascii=False), encoding="utf-8"
            )
        except OSError as exc:
            _log.warning("create_canvas: workspace mirror write failed: %s", exc)


__all__ = ["CreateCanvasTool"]
