"""Client for the optional internal ``doc_to_markdown`` conversion service.

The service preserves Word/PDF structure (tables, formulae and image positions)
better than the orchestrator's lightweight local extractors.  It is an
enhancement, never a hard dependency: callers catch :class:`ConversionError`
and fall back to local extraction.

The inline LLM contract is text-only today.  Image links returned by the
converter therefore become position-preserving text markers before the remote
task is deleted; formulae and Markdown/HTML tables remain intact.
"""

from __future__ import annotations

import re
from urllib.parse import quote

import httpx

from agent_session_orchestrator.services import attachment_text_extractor

_SUPPORTED_EXTS = {".docx", ".doc", ".rtf", ".pdf"}
_SUPPORTED_MIMES = {
    "application/msword",
    "application/pdf",
    "application/rtf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "text/rtf",
}

_MARKDOWN_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\([^\n)]*\)")
_HTML_IMAGE_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
_HTML_ALT_RE = re.compile(r"\balt=[\"']([^\"']*)[\"']", re.IGNORECASE)


class ConversionError(RuntimeError):
    """The rich conversion service could not produce usable Markdown."""


def _extension(filename: str) -> str:
    name = (filename or "").strip().lower()
    dot = name.rfind(".")
    return name[dot:] if dot >= 0 else ""


def supports(filename: str, mime: str) -> bool:
    """Whether the remote converter supports this attachment type."""
    return _extension(filename) in _SUPPORTED_EXTS or (mime or "").lower() in _SUPPORTED_MIMES


def _image_marker(alt: str) -> str:
    label = alt.strip() or "无文字说明"
    return f"【文档图片：{label}】"


def _prepare_text_markdown(markdown: str, *, image_count: int) -> str:
    """Replace expiring image URLs while retaining their document positions."""

    def replace_markdown_image(match: re.Match[str]) -> str:
        return _image_marker(match.group(1))

    def replace_html_image(match: re.Match[str]) -> str:
        alt_match = _HTML_ALT_RE.search(match.group(0))
        return _image_marker(alt_match.group(1) if alt_match else "")

    prepared = _MARKDOWN_IMAGE_RE.sub(replace_markdown_image, markdown)
    prepared = _HTML_IMAGE_RE.sub(replace_html_image, prepared)
    if image_count:
        prepared = (
            prepared.rstrip()
            + f"\n\n> 文档包含 {image_count} 张图片；当前文本对话仅保留图片位置和替代文字，"
            "未传入图片像素。"
        )
    return attachment_text_extractor.truncate_text(prepared)


async def convert(
    *,
    base_url: str,
    filename: str,
    mime: str,
    data: bytes,
    timeout: float,
) -> str:
    """Convert one attachment and return bounded, text-prompt-safe Markdown.

    A successful conversion task is deleted best-effort after its Markdown has
    been read.  Any transport, HTTP, JSON or response-shape problem is surfaced
    as :class:`ConversionError` for the caller's local fallback path.
    """
    if not base_url:
        raise ConversionError("doc_to_markdown service URL is not configured")
    if not supports(filename, mime):
        raise ConversionError("attachment type is not supported by doc_to_markdown")

    root = base_url.rstrip("/")
    task_id: str | None = None
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout)) as client:
            response = await client.post(
                f"{root}/api/convert",
                files={"file": (filename, data, mime or "application/octet-stream")},
                data={
                    "optimize_images": "true",
                    "libreoffice_rasterizer": "true",
                    "detect_equations": "true",
                },
            )
            response.raise_for_status()
            try:
                payload = response.json()
            except ValueError as exc:
                raise ConversionError("doc_to_markdown returned invalid JSON") from exc

            if not isinstance(payload, dict):
                raise ConversionError("doc_to_markdown returned an invalid response")
            raw_task_id = payload.get("task_id")
            if isinstance(raw_task_id, str) and raw_task_id:
                task_id = raw_task_id

            markdown = payload.get("markdown")
            if not isinstance(markdown, str) or not markdown.strip():
                raise ConversionError("doc_to_markdown returned empty Markdown")
            images = payload.get("images")
            image_count = len(images) if isinstance(images, list) else 0
            return _prepare_text_markdown(markdown.strip(), image_count=image_count)
    except ConversionError:
        raise
    except (httpx.HTTPError, ValueError) as exc:
        raise ConversionError(f"doc_to_markdown request failed: {exc}") from exc
    finally:
        if task_id:
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(5.0)) as cleanup_client:
                    await cleanup_client.delete(f"{root}/api/tasks/{quote(task_id, safe='')}")
            except httpx.HTTPError:
                pass
