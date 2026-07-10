"""Block model + materialization helpers for canvas v2 (design §4).

A canvas is stored as an ordered list of blocks::

    [{"id": "blk_a1b2c3d4", "type": "heading"|"paragraph", "text": "..."}]

Two pure functions live here:

* :func:`split_blocks` — deterministic markdown → blocks.  Paragraph split on
  ``\\n{2,}``; every ``#{1,6}``-prefixed line becomes its own ``heading`` block;
  everything else is a ``paragraph``.  Block ids are ``blk_`` + 8 random hex.
* :func:`materialize_blocks` — Phase-A entry point.  Strips only the most
  obvious leading/trailing confirmation chit-chat before splitting, and falls
  back to splitting the *whole* original text if stripping would empty it
  (materialization must never lose content — design invariant §8.5).

Deliberately far more conservative than the frontend's ``canvasObjects.ts``
heuristics: a short, high-precision rule set (better to keep a stray meta line
than to eat real content).
"""

from __future__ import annotations

import re
import uuid

# Match a markdown ATX heading: 1–6 leading '#', then a space, then text.
_HEADING_RE = re.compile(r"^#{1,6}\s+\S")

# Leading confirmation/chit-chat lines the model prepends before the real body.
# High-precision: each pattern anchors on an opener that a *content* line almost
# never starts with.  Kept intentionally short (design §4: "宁少勿多").
_LEADING_META_RES = (
    # "好的，我来…" / "当然，下面…" / "没问题，这就…" style acknowledgements.
    re.compile(
        r"^(?:好的|当然|当然可以|可以|没问题|收到|明白)[，,。\s]*"
        r"(?:我来|我会|下面|以下|为你|给你|这就|这里|这是)"
    ),
    # "下面是这篇文章：" / "以下为报告正文" style hand-offs.
    re.compile(
        r"^(?:下面|以下|这里|这是|为你|给你).{0,30}"
        r"(?:文章|稿件|讲话|草稿|正文|版本|内容|邮件|报告|方案)"
    ),
)

# Trailing meta labels the model appends after the body ("说明：…", "备注：…").
_TRAILING_META_LABEL_RE = re.compile(
    r"^(?:说明|备注|注|使用建议|修改建议|温馨提示)\s*[:：]"
)


def _blk_id() -> str:
    return f"blk_{uuid.uuid4().hex[:8]}"


def split_blocks(markdown: str) -> list[dict[str, str]]:
    """Split ``markdown`` into ordered ``{id, type, text}`` blocks.

    Rules (design §4):

    * Paragraphs are separated by two or more consecutive newlines.
    * Within a paragraph, any line matching ``#{1,6}\\s`` is a standalone
      ``heading`` block (a heading terminates the surrounding paragraph run).
    * Everything else is a ``paragraph`` block.  List / code content is left
      whole as a paragraph for now (Tiptap extension point).

    Empty input yields an empty list.
    """
    text = (markdown or "").strip()
    if not text:
        return []

    blocks: list[dict[str, str]] = []
    for chunk in re.split(r"\n{2,}", text):
        chunk = chunk.strip("\n")
        if chunk.strip():
            blocks.extend(_split_chunk(chunk))
    return blocks


def _split_chunk(chunk: str) -> list[dict[str, str]]:
    """Split one paragraph chunk, hoisting heading lines into their own blocks.

    A heading line inside a chunk breaks the surrounding prose so the heading
    stands alone and the prose stays in its own paragraph block(s).
    """
    out: list[dict[str, str]] = []
    buffer: list[str] = []

    def flush() -> None:
        joined = "\n".join(buffer).strip()
        buffer.clear()
        if joined:
            out.append({"id": _blk_id(), "type": "paragraph", "text": joined})

    for line in chunk.split("\n"):
        if _HEADING_RE.match(line.strip()):
            flush()
            out.append({"id": _blk_id(), "type": "heading", "text": line.strip()})
        else:
            buffer.append(line)
    flush()
    return out


def _strip_meta(markdown: str) -> str:
    """Drop obvious leading/trailing confirmation chit-chat (conservative).

    Operates on ``\\n{2,}``-separated paragraphs so it never bisects a real
    paragraph.  Only removes a boundary paragraph whose *first* line matches a
    high-precision meta pattern.  Returns the trimmed remainder (may be empty —
    the caller handles the empty-strip fallback).
    """
    paragraphs = [p.strip() for p in re.split(r"\n{2,}", markdown.strip()) if p.strip()]

    def is_leading_meta(paragraph: str) -> bool:
        first = paragraph.split("\n", 1)[0].strip()
        return any(rx.match(first) for rx in _LEADING_META_RES)

    def is_trailing_meta(paragraph: str) -> bool:
        first = paragraph.split("\n", 1)[0].strip()
        return bool(_TRAILING_META_LABEL_RE.match(first))

    # Strip at most a couple of leading meta paragraphs (never the whole doc).
    while len(paragraphs) > 1 and is_leading_meta(paragraphs[0]):
        paragraphs.pop(0)
    while len(paragraphs) > 1 and is_trailing_meta(paragraphs[-1]):
        paragraphs.pop()

    return "\n\n".join(paragraphs).strip()


def materialize_blocks(markdown: str) -> list[dict[str, str]]:
    """Phase-A materialization: strip boundary chit-chat, then split.

    Fail-open on content (design §8.5): if stripping empties the text, fall
    back to splitting the *original* whole text so no content is ever lost.
    """
    original = (markdown or "").strip()
    if not original:
        return []
    stripped = _strip_meta(original)
    blocks = split_blocks(stripped)
    if not blocks:
        # Stripping (or splitting) produced nothing — never drop content.
        return split_blocks(original)
    return blocks
