"""Unit tests for parsers + chunker."""

from __future__ import annotations

import pytest

from knowledge_ingest.errors import UnsupportedMimeError
from knowledge_ingest.parsers.base import chunk_text, parse_document


def test_parse_plain_text() -> None:
    assert parse_document(mime="text/plain", data=b"hello world") == "hello world"


def test_parse_markdown() -> None:
    md = b"# Title\n\nsome text"
    assert parse_document(mime="text/markdown", data=md).startswith("# Title")


def test_parse_unsupported_raises() -> None:
    with pytest.raises(UnsupportedMimeError):
        parse_document(mime="image/png", data=b"\x89PNG")


def test_chunk_text_small() -> None:
    chunks = chunk_text("hi", chunk_size=10, overlap=2)
    assert chunks == ["hi"]


def test_chunk_text_window() -> None:
    text = "a" * 25
    chunks = chunk_text(text, chunk_size=10, overlap=2)
    # step = 8 → starts at 0, 8, 16, 24
    assert len(chunks) >= 3
    assert all(len(c) <= 10 for c in chunks)


def test_chunk_text_overlap_validation() -> None:
    with pytest.raises(ValueError):
        chunk_text("hello", chunk_size=10, overlap=10)
    with pytest.raises(ValueError):
        chunk_text("hello", chunk_size=0, overlap=0)
