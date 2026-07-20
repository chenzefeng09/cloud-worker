"""Tests for the optional rich document-conversion client."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from agent_session_orchestrator.services import doc_to_markdown_client as client

_TEST_LOOP = asyncio.new_event_loop()


class _FakeAsyncClient:
    responses: list[httpx.Response] = []
    posts: list[dict[str, Any]] = []
    deletes: list[str] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.timeout = kwargs.get("timeout")

    async def __aenter__(self) -> _FakeAsyncClient:
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        type(self).posts.append({"url": url, **kwargs})
        return type(self).responses.pop(0)

    async def delete(self, url: str, **kwargs: Any) -> httpx.Response:
        type(self).deletes.append(url)
        return httpx.Response(200, json={"deleted": True}, request=httpx.Request("DELETE", url))


@pytest.fixture(autouse=True)
def _reset_fake_client(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeAsyncClient.responses = []
    _FakeAsyncClient.posts = []
    _FakeAsyncClient.deletes = []
    monkeypatch.setattr(client.httpx, "AsyncClient", _FakeAsyncClient)


def _response(status: int, payload: Any) -> httpx.Response:
    request = httpx.Request("POST", "http://doc-to-md:8000/api/convert")
    return httpx.Response(status, json=payload, request=request)


def _run(coro: Any) -> Any:
    """Run without closing the process-wide loop expected by legacy tests."""
    asyncio.set_event_loop(_TEST_LOOP)
    return _TEST_LOOP.run_until_complete(coro)


def test_supports_word_pdf_but_not_plain_or_spreadsheets() -> None:
    assert client.supports("report.docx", "application/octet-stream") is True
    assert client.supports("legacy.DOC", "application/octet-stream") is True
    assert client.supports("scan.bin", "application/pdf") is True
    assert client.supports("notes.txt", "text/plain") is False
    assert client.supports("book.xlsx", "application/octet-stream") is False


def test_convert_preserves_markdown_and_cleans_up_task() -> None:
    _FakeAsyncClient.responses = [
        _response(
            200,
            {
                "task_id": "task/with space",
                "markdown": (
                    "# 报告\n\n![架构图](media/diagram.png)\n\n"
                    "公式：$x^2 + y^2$\n\n| 项目 | 数值 |\n|---|---|\n| A | 1 |"
                ),
                "images": [{"name": "diagram.png"}],
            },
        )
    ]

    markdown = _run(
        client.convert(
            base_url="http://doc-to-md:8000/",
            filename="report.docx",
            mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            data=b"docx bytes",
            timeout=42,
        )
    )

    assert "# 报告" in markdown
    assert "【文档图片：架构图】" in markdown
    assert "公式：$x^2 + y^2$" in markdown
    assert "| 项目 | 数值 |" in markdown
    assert "文档包含 1 张图片" in markdown
    assert "media/diagram.png" not in markdown
    assert _FakeAsyncClient.posts[0]["url"] == "http://doc-to-md:8000/api/convert"
    assert _FakeAsyncClient.posts[0]["files"]["file"] == (
        "report.docx",
        b"docx bytes",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
    assert _FakeAsyncClient.posts[0]["data"]["detect_equations"] == "true"
    assert _FakeAsyncClient.deletes == [
        "http://doc-to-md:8000/api/tasks/task%2Fwith%20space"
    ]


def test_http_failure_becomes_conversion_error_without_cleanup() -> None:
    _FakeAsyncClient.responses = [_response(503, {"detail": "unavailable"})]

    with pytest.raises(client.ConversionError, match="request failed"):
        _run(
            client.convert(
                base_url="http://doc-to-md:8000",
                filename="report.pdf",
                mime="application/pdf",
                data=b"pdf",
                timeout=1,
            )
        )

    assert _FakeAsyncClient.deletes == []


def test_empty_markdown_is_rejected_but_task_is_cleaned_up() -> None:
    _FakeAsyncClient.responses = [
        _response(200, {"task_id": "task-empty", "markdown": " ", "images": []})
    ]

    with pytest.raises(client.ConversionError, match="empty Markdown"):
        _run(
            client.convert(
                base_url="http://doc-to-md:8000",
                filename="empty.rtf",
                mime="application/rtf",
                data=b"rtf",
                timeout=1,
            )
        )

    assert _FakeAsyncClient.deletes == [
        "http://doc-to-md:8000/api/tasks/task-empty"
    ]
