"""MIME-aware document parsing + chunking.

Allowed MIMEs
-------------
* ``text/plain``    — decoded as UTF-8, splitter runs directly.
* ``text/markdown`` — decoded as UTF-8 (the chunker is markdown-naive on
                      purpose; markdown structure is preserved as raw text).
* ``application/pdf`` — page-by-page extraction via ``pypdf``; pages are
                        joined with double newlines before chunking.

Anything else raises :class:`UnsupportedMimeError` so the API surface can
short-circuit with a 415 before we waste blob storage.
"""

from __future__ import annotations

import io

from knowledge_ingest.errors import UnsupportedMimeError

ALLOWED_TEXT_MIMES = {"text/plain", "text/markdown"}
PDF_MIME = "application/pdf"


def parse_document(*, mime: str, data: bytes) -> str:
    """Return the full plain-text body of ``data`` (post-decoding)."""
    if mime in ALLOWED_TEXT_MIMES:
        return data.decode("utf-8", errors="replace")
    if mime == PDF_MIME:
        return _parse_pdf(data)
    raise UnsupportedMimeError(mime)


def _parse_pdf(data: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    pages = [page.extract_text() or "" for page in reader.pages]
    return "\n\n".join(pages)


def chunk_text(text: str, *, chunk_size: int = 800, overlap: int = 80) -> list[str]:
    """Split ``text`` into sliding windows of ~chunk_size characters.

    The splitter is byte-naive (character-based, not token-based) — that's
    fine for the skeleton; a future revision can swap in a token-aware
    splitter without touching the call sites.
    """
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    if overlap < 0 or overlap >= chunk_size:
        raise ValueError("overlap must satisfy 0 <= overlap < chunk_size")

    text = text.strip()
    if not text:
        return []

    step = chunk_size - overlap
    chunks: list[str] = []
    for start in range(0, len(text), step):
        piece = text[start : start + chunk_size].strip()
        if piece:
            chunks.append(piece)
        if start + chunk_size >= len(text):
            break
    return chunks
