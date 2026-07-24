"""Tests for canvas block utilities (split_blocks / materialize_blocks) and
the pure diff-application helper (apply_diffs)."""

from __future__ import annotations

from agent_session_orchestrator.services.canvas_blocks import (
    materialize_blocks,
    single_html_block,
    split_blocks,
)
from agent_session_orchestrator.services.canvas_service import apply_diffs

# ── single_html_block ────────────────────────────────────────────────────────


def test_single_html_block_wraps_verbatim() -> None:
    html = "<html>\n<body>\n<h1>hi</h1>\n</body>\n</html>"
    blocks = single_html_block(html)
    assert len(blocks) == 1
    assert blocks[0]["type"] == "html"
    # Stored byte-for-byte — not split on the embedded newlines.
    assert blocks[0]["text"] == html
    assert blocks[0]["id"].startswith("blk_")


def test_single_html_block_blank_is_empty() -> None:
    assert single_html_block("") == []
    assert single_html_block("   \n  ") == []


# ── split_blocks ─────────────────────────────────────────────────────────────


def test_split_blocks_empty_returns_empty() -> None:
    assert split_blocks("") == []
    assert split_blocks("   \n\n  ") == []


def test_split_blocks_single_paragraph() -> None:
    blocks = split_blocks("hello world")
    assert len(blocks) == 1
    assert blocks[0]["type"] == "paragraph"
    assert blocks[0]["text"] == "hello world"
    assert blocks[0]["id"].startswith("blk_")
    assert len(blocks[0]["id"]) == len("blk_") + 8


def test_split_blocks_multiple_paragraphs() -> None:
    blocks = split_blocks("first para\n\nsecond para\n\n\nthird para")
    assert [b["text"] for b in blocks] == ["first para", "second para", "third para"]
    assert all(b["type"] == "paragraph" for b in blocks)


def test_split_blocks_heading_is_standalone_block() -> None:
    md = "# Title\n\nBody paragraph here."
    blocks = split_blocks(md)
    assert len(blocks) == 2
    assert blocks[0]["type"] == "heading"
    assert blocks[0]["text"] == "# Title"
    assert blocks[1]["type"] == "paragraph"


def test_split_blocks_heading_inside_chunk_breaks_paragraph() -> None:
    # A heading line sharing a chunk with prose splits into 3 blocks.
    md = "intro line\n## Section\ntrailing line"
    blocks = split_blocks(md)
    assert [b["type"] for b in blocks] == ["paragraph", "heading", "paragraph"]
    assert blocks[0]["text"] == "intro line"
    assert blocks[1]["text"] == "## Section"
    assert blocks[2]["text"] == "trailing line"


def test_split_blocks_all_six_heading_levels() -> None:
    for level in range(1, 7):
        prefix = "#" * level
        blocks = split_blocks(f"{prefix} Heading")
        assert blocks[0]["type"] == "heading"
    # 7 '#' is not a valid ATX heading → paragraph
    blocks = split_blocks("####### not a heading")
    assert blocks[0]["type"] == "paragraph"


def test_split_blocks_ids_are_unique() -> None:
    blocks = split_blocks("a\n\nb\n\nc\n\nd")
    ids = [b["id"] for b in blocks]
    assert len(ids) == len(set(ids))


# ── materialize_blocks ───────────────────────────────────────────────────────


def test_materialize_strips_leading_ack() -> None:
    md = "好的，我来为你写一篇文章。\n\n# 正文标题\n\n这是正文内容。"
    blocks = materialize_blocks(md)
    texts = [b["text"] for b in blocks]
    assert "好的，我来为你写一篇文章。" not in texts
    assert "# 正文标题" in texts
    assert "这是正文内容。" in texts


def test_materialize_strips_trailing_meta_label() -> None:
    md = "正文第一段。\n\n正文第二段。\n\n说明：以上内容仅供参考。"
    blocks = materialize_blocks(md)
    texts = [b["text"] for b in blocks]
    assert "正文第一段。" in texts
    assert not any(t.startswith("说明：") for t in texts)


def test_materialize_pure_meta_falls_back_to_whole() -> None:
    # A single meta-only line: stripping would empty it, so it must fall back
    # to splitting the original whole text (never lose content).
    md = "好的，我来为你写。"
    blocks = materialize_blocks(md)
    assert len(blocks) == 1
    assert blocks[0]["text"] == "好的，我来为你写。"


def test_materialize_empty_returns_empty() -> None:
    assert materialize_blocks("") == []
    assert materialize_blocks("   ") == []


def test_materialize_keeps_real_content_untouched() -> None:
    md = "# 报告\n\n第一段实质内容。\n\n第二段实质内容。"
    blocks = materialize_blocks(md)
    assert [b["text"] for b in blocks] == [
        "# 报告",
        "第一段实质内容。",
        "第二段实质内容。",
    ]


# ── apply_diffs ──────────────────────────────────────────────────────────────


def _blocks() -> list[dict]:
    return [
        {"id": "blk_1", "type": "heading", "text": "# Title"},
        {"id": "blk_2", "type": "paragraph", "text": "first"},
        {"id": "blk_3", "type": "paragraph", "text": "second"},
    ]


def test_apply_diffs_replace_keeps_id() -> None:
    diffs = [{"block_id": "blk_2", "op": "replace", "orig": "first", "sugg": "FIRST!"}]
    out = apply_diffs(_blocks(), diffs)
    assert [b["id"] for b in out] == ["blk_1", "blk_2", "blk_3"]
    assert out[1]["text"] == "FIRST!"
    assert out[1]["id"] == "blk_2"  # id stability


def test_apply_diffs_delete_removes_block() -> None:
    diffs = [{"block_id": "blk_2", "op": "delete"}]
    out = apply_diffs(_blocks(), diffs)
    assert [b["id"] for b in out] == ["blk_1", "blk_3"]


def test_apply_diffs_insert_after_gets_new_id() -> None:
    diffs = [{"block_id": "blk_1", "op": "insert_after", "sugg": "inserted body"}]
    out = apply_diffs(_blocks(), diffs)
    assert len(out) == 4
    assert out[0]["id"] == "blk_1"
    assert out[1]["text"] == "inserted body"
    assert out[1]["id"].startswith("blk_")
    assert out[1]["id"] not in {"blk_1", "blk_2", "blk_3"}
    # Untouched blocks keep their ids and order after the insertion.
    assert [b["id"] for b in out[2:]] == ["blk_2", "blk_3"]


def test_apply_diffs_replace_to_heading_infers_type() -> None:
    diffs = [{"block_id": "blk_2", "op": "replace", "sugg": "## New Heading"}]
    out = apply_diffs(_blocks(), diffs)
    assert out[1]["type"] == "heading"
    assert out[1]["id"] == "blk_2"


def test_apply_diffs_unknown_block_id_ignored() -> None:
    diffs = [{"block_id": "blk_999", "op": "replace", "sugg": "ghost"}]
    out = apply_diffs(_blocks(), diffs)
    assert out == _blocks()


def test_apply_diffs_untouched_ids_stable() -> None:
    diffs = [{"block_id": "blk_2", "op": "replace", "sugg": "changed"}]
    out = apply_diffs(_blocks(), diffs)
    # blk_1 and blk_3 must be byte-identical to the input.
    assert out[0] == {"id": "blk_1", "type": "heading", "text": "# Title"}
    assert out[2] == {"id": "blk_3", "type": "paragraph", "text": "second"}
