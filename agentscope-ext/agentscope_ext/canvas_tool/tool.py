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

    This tool is REQUIRED for every long-form written-creation request.  If the
    user asks you to WRITE a piece of prose — an article, essay, short piece,
    report, brief, proposal, email, marketing copy, summary, speech, story,
    social post, code note, or any titled / 《书名号》 writing assignment — you
    MUST call this tool FIRST and hand the FULL finished body to
    ``content_markdown``.  This is the ONLY correct delivery path for such
    content: do not write the article in the reply body instead of, or in
    addition to, calling this tool.  The tool captures the finished content as a
    versioned canvas the user can then edit block by block.

    Do NOT call this tool for ordinary conversation: short answers,
    explanations, quick lists, calculations, code debugging, code snippets shown
    for reading, or anything where the user is not asking for a written work.

    After you call this tool, keep your natural-language reply to ONE short
    sentence telling the user the document is in the canvas (e.g. "已为你创作
    《…》，内容在右侧画布中，可查看和编辑。").  Do NOT repeat the full content in
    the reply — it already lives in the canvas.

    Args:
        kind: The document category.  One of ``article``, ``brief``, ``email``,
            ``report``, ``proposal``, ``social_post``, ``code_note``.  Defaults
            to ``article`` if unsure.
        title: A concise human-readable title for the document, extracted from
            the user's assignment / 《书名号》 when given (used as the canvas
            heading and in listings).
        content_markdown: The COMPLETE finished document body in Markdown — the
            whole piece, not a summary or outline.  Use ``#`` / ``##`` headings
            and blank-line-separated paragraphs; the server splits this into
            editable blocks.

    Returns:
        A short confirmation string that names the created canvas by title.
    """

    name = "create_canvas"

    description: str = (
        "REQUIRED for ALL long-form written creation. Whenever the user asks you "
        "to WRITE a piece of prose — an article, essay, short piece, report, "
        "brief, proposal, email, marketing copy, summary, speech, story, social "
        "post, code note, or any titled/《书名号》 writing assignment — you MUST "
        "call this tool FIRST and put the FULL finished body into "
        "content_markdown. This is the only correct way to deliver such content; "
        "do not write the article in the reply body instead of, or in addition "
        "to, calling this tool. After calling it, reply with just ONE short "
        "sentence saying the document is in the canvas — never repeat the "
        "content. You may ALSO use this tool to deliver a RUNNABLE web page/app "
        "— a small game, interactive widget, animation, or data visualization: "
        "set content_type='html' and put a COMPLETE self-contained HTML document "
        "(inline <style>/<script>, no external CDN or network) into "
        "content_markdown; the canvas runs it live in a sandboxed preview. "
        "Do NOT call for ordinary conversation: short answers, "
        "explanations, lists, calculations, code debugging, or code snippets "
        "shown for reading — i.e. when the user is not asking for a written work."
    )

    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "kind": {
                "type": "string",
                "description": (
                    "Document category, chosen by the piece's genre: 'article' "
                    "(essay / short piece / general prose), 'brief', 'email', "
                    "'report', 'proposal', 'social_post', 'code_note'. Defaults "
                    "to 'article' when unsure."
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
                "description": (
                    "A concise human-readable title for the document. Extract it "
                    "from the user's assignment or 《书名号》 when one is given."
                ),
            },
            "content_markdown": {
                "type": "string",
                "description": (
                    "The COMPLETE finished document body. For prose "
                    "(content_type='markdown') use '#'/'##' headings and "
                    "blank-line-separated paragraphs; the server splits it into "
                    "editable blocks. For content_type='html', put a COMPLETE "
                    "self-contained HTML document here instead (inline "
                    "<style>/<script>, no external resources) — it is stored and "
                    "run verbatim, never split."
                ),
            },
            "content_type": {
                "type": "string",
                "description": (
                    "Content format. 'markdown' (default) for written prose. Use "
                    "'html' when the user asks for a RUNNABLE / INTERACTIVE web "
                    "thing — a small game, widget, animation, data visualization, "
                    "or web page they can actually use — and put a complete "
                    "self-contained HTML document into content_markdown; the "
                    "canvas runs it in a sandboxed preview."
                ),
                "enum": ["markdown", "html"],
                "default": "markdown",
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
        content_type: Optional[str] = "markdown",
    ) -> Any:
        text = self._create(
            title=title,
            content_markdown=content_markdown,
            kind=kind,
            content_type=content_type,
        )
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
        content_type: Optional[str] = "markdown",
    ) -> str:
        payload = {
            "kind": (kind or "article"),
            "title": title or "未命名画布",
            "content_markdown": content_markdown or "",
            "content_type": (content_type or "markdown"),
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
