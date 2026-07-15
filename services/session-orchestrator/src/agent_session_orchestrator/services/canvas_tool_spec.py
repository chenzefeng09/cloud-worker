"""Inline-path canvas tool spec — the OpenAI ``create_canvas`` contract.

Background
----------
On the **sandbox** path the ``create_canvas`` tool, its JSON schema and the
forcing system-prompt block all live inside the ``agentscope_ext`` package that
is baked into the worker image (``agentscope_ext.canvas_tool.tool.CreateCanvasTool``
and the prompt block in ``runtime_worker.__main__``).  That package is **not**
importable from this service (it is not a dependency of session-orchestrator and
is not published as a shared lib), so the inline path — which advertises the tool
to the gateway as an OpenAI ``tools=[…]`` function and calls it in-process —
cannot reuse those symbols directly.

This module is therefore the **single source of truth for the inline path**: the
OpenAI function schema below mirrors ``CreateCanvasTool.input_schema`` field for
field (``kind`` enum, ``title`` / ``content_markdown`` required), and the forcing
prompt mirrors the worker's canvas system-prompt block.  Keeping the schema and
prompt here (rather than inline in ``inline_run_executor`` or ``build_openai_messages``)
means every inline caller imports the same definition instead of copy-pasting it.

When to keep this in sync with the sandbox tool
-----------------------------------------------
If ``CreateCanvasTool.input_schema`` changes (a new ``kind`` value, a renamed
field), update :data:`CREATE_CANVAS_TOOL` here too — the two must stay aligned so
a canvas materialized from an inline tool call is indistinguishable from a
sandbox one (same ``kind`` set, same ``content_markdown`` → ``content`` mapping in
``CanvasService.create_canvas``).
"""

from __future__ import annotations

from typing import Any

# The OpenAI tool name — identical to ``CreateCanvasTool.name`` so an inline
# tool_call and a sandbox tool call are the same tool as far as materialization,
# audit and the frontend are concerned.
CREATE_CANVAS_TOOL_NAME = "create_canvas"

# The seven canvas document categories, mirrored from
# ``agentscope_ext.canvas_tool.tool.CreateCanvasTool.input_schema`` — keep in sync.
CANVAS_KINDS: list[str] = [
    "article",
    "brief",
    "email",
    "report",
    "proposal",
    "social_post",
    "code_note",
]

# The OpenAI ``tools=[…]`` entry advertised to the gateway for an inline run.
# ``function.parameters`` mirrors ``CreateCanvasTool.input_schema`` exactly so the
# arguments the model returns map straight onto ``CanvasService.create_canvas``
# (``content_markdown`` → ``content``, ``kind``, ``title``).  The description
# carries the same REQUIRED/MUST-FIRST forcing framing the sandbox tool uses so a
# weak-instruction model (deepseek) reliably fires the tool for prose.
CREATE_CANVAS_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": CREATE_CANVAS_TOOL_NAME,
        "description": (
            "REQUIRED for ALL long-form written creation. Whenever the user asks "
            "you to WRITE a piece of prose — an article, essay, short piece, "
            "report, brief, proposal, email, marketing copy, summary, speech, "
            "story, social post, code note, or any titled/《书名号》 writing "
            "assignment — you MUST call this tool FIRST and put the FULL finished "
            "body into content_markdown. This is the only correct way to deliver "
            "such content; do not write the article in the reply body instead of, "
            "or in addition to, calling this tool. After calling it, reply with "
            "just ONE short sentence saying the document is in the canvas — never "
            "repeat the content. Do NOT call for ordinary conversation: short "
            "answers, explanations, lists, calculations, code debugging, or code "
            "snippets shown for reading — i.e. when the user is not asking for a "
            "written work. If for ANY reason you do not call this tool, you MUST "
            "write the complete text directly in your reply instead — NEVER claim "
            "the content is in a canvas you did not actually create."
        ),
        "parameters": {
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
                    "enum": list(CANVAS_KINDS),
                    "default": "article",
                },
                "title": {
                    "type": "string",
                    "description": (
                        "A concise human-readable title for the document. Extract "
                        "it from the user's assignment or 《书名号》 when one is given."
                    ),
                },
                "content_markdown": {
                    "type": "string",
                    "description": (
                        "The COMPLETE finished document body in Markdown — the "
                        "whole piece, not a summary or outline. Use '#'/'##' "
                        "headings and blank-line-separated paragraphs; the server "
                        "splits this into editable blocks."
                    ),
                },
            },
            "required": ["title", "content_markdown"],
        },
    },
}

# The forcing system-prompt block, mirrored from the worker's canvas block in
# ``runtime_worker.__main__`` (the ``## 长文成文创作`` section) so an inline model
# reads the exact same hard-rule framing the sandbox model does.  Appended to the
# inline system message only when the canvas tool is enabled (the tool and the
# instruction appear/disappear together, matching the sandbox gating).
CANVAS_FORCING_PROMPT = (
    "\n\n## 长文成文创作：必须用画布（create_canvas）\n"
    "这是一条硬性规则。凡是「成文创作类」请求——"
    "写文章、短文、报告、简报、方案、邮件、文案、总结、"
    "演讲稿、故事、社媒长文、代码笔记等，"
    "以及任何《书名号》命题写作——"
    "唯一正确的做法是：先调用 create_canvas 工具，"
    "把标题（从命题或书名号里提取）和**完整正文**放进 content_markdown。\n"
    "禁止：不要先在正文里把文章写出来再调用；"
    "不要一边调用一边在正文重复全文。"
    "调用工具后，你的正文回复**只能写一句话**说明，"
    "例如「已为你创作《XX》，内容在右侧画布中，可查看和编辑」。\n"
    "只有这些才不调用、直接在正文回复："
    "闲聊、简短问答、解释说明、清单、计算、代码调试、"
    "供阅读的短代码片段——即用户并非要一篇成文作品时。\n"
    "兜底规则：如果这一轮你（因任何原因）没有调用 create_canvas，"
    "就必须把**完整正文**直接写在回复里交付给用户；"
    "**严禁**在未实际调用工具的情况下说「内容在画布中」——"
    "那会让用户面对一个不存在的画布。\n"
    "示例：用户说「写一篇《AI 与就业》短文，约300字」"
    "→ 立即调用 create_canvas（title=「AI 与就业」，"
    "content_markdown=完整300字正文），正文只回一句说明。"
)


# ── Written-creation heuristic (weak-instruction forcing) ────────────────────
# Precedent / important caveat: commit 5447b43 ("force create_canvas for
# written-creation prompts on weak-instruction models") did NOT introduce a
# server-side classifier or a weak-model allowlist — it hardened the worker's
# canvas prompt + tool description so the LLM itself decides.  There is therefore
# no shared wordlist to import; the sandbox path relies purely on prompt forcing.
#
# For the inline path we additionally hold a small, self-contained heuristic here
# so that on the FIRST completion round we can pin ``tool_choice`` to
# ``create_canvas`` for prompts that are unambiguously written-creation requests.
# This is a belt-and-braces measure for weak-instruction models (deepseek fires
# the tool only probabilistically even under strong wording): a hard tool_choice
# guarantees the first round produces the canvas, and the second round drops back
# to ``auto`` so the model writes its one-sentence pointer reply.  The heuristic
# is deliberately conservative (it only forces on explicit 写…/创作… + genre
# phrasing) — a miss simply falls back to the prompt-driven ``auto`` behaviour,
# never a wrong forced canvas.

# Verbs that head a written-creation instruction ("写一篇…", "创作一首…", "起草…").
_WRITE_VERBS = ("写", "creat", "compose", "draft", "起草", "撰写", "创作", "拟")

# Genre nouns that mark the request as a standalone written work.  Mirrors the
# genre list in the worker prompt / tool description so the two surfaces cover the
# same request shapes.
_WRITTEN_GENRES = (
    "文章", "短文", "报告", "简报", "方案", "邮件", "文案", "总结",
    "演讲稿", "故事", "小说", "散文", "诗", "作文", "社媒", "笔记",
    "article", "essay", "report", "brief", "proposal", "email",
    "story", "post", "speech",
)


def is_written_creation_prompt(task: str) -> bool:
    """Heuristic: does ``task`` read as an explicit written-creation request?

    True only when the prompt combines a write-verb with a written-work genre (or
    carries a 《书名号》 titled assignment) — e.g. "写一篇关于AI的短文",
    "帮我创作一封道歉邮件", "draft a report on Q3".  Conservative by design: used
    to pin ``tool_choice=create_canvas`` on the first inline round, so a false
    positive would force an unwanted canvas; a false negative merely falls back to
    the prompt-forced ``auto`` behaviour the sandbox path also relies on.
    """
    if not task:
        return False
    text = task.strip()
    if not text:
        return False
    lowered = text.lower()

    has_verb = any(v in lowered for v in _WRITE_VERBS)
    if not has_verb:
        return False

    # A 《书名号》 titled assignment alongside a write-verb is a written work.
    if "《" in text and "》" in text:
        return True

    return any(g in lowered for g in _WRITTEN_GENRES)


def extract_creation_title(task: str) -> str:
    """Best-effort title for a written-creation ``task`` (used by the inline rescue).

    Prefers the first 《书名号》 title when the prompt carries one (the命题写作 name
    the user gave); otherwise falls back to the whitespace-stripped prompt truncated
    to 30 chars.  Never empty for a non-blank task — the rescue always has a title to
    hand to ``create_canvas`` even when the model produced none.
    """
    text = (task or "").strip()
    if not text:
        return "未命名"
    start = text.find("《")
    if start != -1:
        end = text.find("》", start + 1)
        if end != -1 and end > start + 1:
            inner = text[start + 1 : end].strip()
            if inner:
                return inner
    collapsed = " ".join(text.split())
    return collapsed[:30]


def canvas_confirmation_reply(title: str | None) -> str:
    """The short tool-result confirmation fed back to the model after a create.

    Mirrors the sandbox tool's one-line confirmation so the model, on the second
    round, produces the same "已为你创作《…》…" pointer reply.  ``title`` may be
    ``None`` when the model omitted it (rare — it is a required field).
    """
    name = (title or "").strip() or "未命名画布"
    return (
        f"已创建画布《{name}》，完整内容已存入右侧画布，可查看和编辑。"
        "请只用一句话告诉用户画布已创建，不要在正文重复全文。"
    )
