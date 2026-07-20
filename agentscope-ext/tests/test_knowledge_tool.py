"""Tests for ``agentscope_ext.knowledge_tool``.

Covers:
- SearchKnowledgeTool sends GET {base}/search?q=&k=&scope=knowledge with the
  three identity headers (X-Tenant-Id / X-User-Id / X-User-Clearance) and the
  optional X-Tool-Broker-Lease, and formats hits as
  ``【文档名 | 文件夹路径】\\n<text>``.
- Fail-open: unreachable / non-200 / unparseable -> short "service unavailable"
  note; no hits -> "未检索到" note.  Never raises.
- Empty query short-circuits.
- install() wraps hooks.build_toolkit and appends the tool when
  KNOWLEDGE_INGEST_URL is set; passes env-derived identity + lease to the tool.
- install() is idempotent.
- install() skips tool registration (info log) when KNOWLEDGE_INGEST_URL is
  absent (fail-open for deployments without ki).
- bootstrap.enable_from_env activates knowledge_tool by name.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_PKG_ROOT = Path(__file__).resolve().parent.parent
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))


# ---------------------------------------------------------------------------
# Toolkit / ToolGroup stubs (same pattern as test_web_fetch.py)
# ---------------------------------------------------------------------------


class _FakeToolGroup:
    def __init__(self):
        self.name = "basic"
        self.tools: list = []
        self.mcps: list = []


class _FakeToolkit:
    def __init__(self):
        self.tool_groups = [_FakeToolGroup()]

    @property
    def tools(self):
        return self.tool_groups[0].tools


# ---------------------------------------------------------------------------
# SearchKnowledgeTool unit tests (mock httpx.get)
# ---------------------------------------------------------------------------


class TestSearchKnowledgeTool:
    """Direct tests against SearchKnowledgeTool.__call__ with mocked httpx."""

    def _make_tool(self, **kw):
        from agentscope_ext.knowledge_tool.tool import SearchKnowledgeTool

        defaults = dict(
            base_url="http://fake-ki:8012/api/knowledge/v1",
            tenant_id="tnt-kb",
            user_id="usr-kb",
            clearance="L3",
            broker_lease="",
        )
        defaults.update(kw)
        return SearchKnowledgeTool(**defaults)

    def _hit(self, name, folder, text):
        return {
            "document_name": name,
            "folder_path": folder,
            "text": text,
            "chunk_id": "c1",
            "document_id": "d1",
            "score": 0.9,
            "scope": "knowledge",
        }

    def test_success_formats_hits_with_provenance(self):
        """HTTP 200 with hits -> count header + 【name | folder】\\n<text> blocks."""
        tool = self._make_tool()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "hits": [
                self._hit("合同模板.docx", "产品/合同", "甲方应..."),
                self._hit("规章制度.pdf", "行政", "考勤规定..."),
            ]
        }
        with patch("httpx.get", return_value=mock_resp) as mock_get:
            result = tool("合同模板")

        text = result if isinstance(result, str) else " ".join(
            str(c) for c in getattr(result, "content", [])
        )
        assert "在租户知识库中检索到 2 条相关资料" in text
        assert "【合同模板.docx | 产品/合同】" in text
        assert "甲方应..." in text
        assert "【规章制度.pdf | 行政】" in text

        # Query/k/scope params + identity headers.
        _, kwargs = mock_get.call_args
        assert mock_get.call_args.args[0].endswith("/search")
        assert kwargs["params"]["q"] == "合同模板"
        assert kwargs["params"]["k"] == 5
        assert kwargs["params"]["scope"] == "knowledge"
        assert kwargs["headers"]["X-Tenant-Id"] == "tnt-kb"
        assert kwargs["headers"]["X-User-Id"] == "usr-kb"
        assert kwargs["headers"]["X-User-Clearance"] == "L3"
        assert "X-Tool-Broker-Lease" not in kwargs["headers"]

    def test_folder_omitted_at_root(self):
        """Empty folder_path -> 【name】 without the | separator."""
        tool = self._make_tool()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"hits": [self._hit("root.doc", "", "body")]}
        with patch("httpx.get", return_value=mock_resp):
            result = tool("q")
        text = result if isinstance(result, str) else str(result.content)
        assert "【root.doc】" in text
        assert "|" not in text.split("】")[0]

    def test_no_hits_returns_not_found_note(self):
        """200 with empty hits -> 未检索到 note."""
        tool = self._make_tool()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"hits": []}
        with patch("httpx.get", return_value=mock_resp):
            result = tool("冷门词")
        text = result if isinstance(result, str) else str(result.content)
        assert "未在租户知识库中检索到" in text
        assert "冷门词" in text

    def test_non_200_returns_unavailable_note(self):
        """Non-200 -> service unavailable note (fail-open)."""
        tool = self._make_tool()
        mock_resp = MagicMock()
        mock_resp.status_code = 503
        with patch("httpx.get", return_value=mock_resp):
            result = tool("q")
        text = result if isinstance(result, str) else str(result.content)
        assert "知识库检索服务暂时不可用" in text

    def test_connect_error_returns_unavailable_note(self):
        """ConnectError -> service unavailable note (fail-open, no raise)."""
        import httpx

        tool = self._make_tool()
        with patch("httpx.get", side_effect=httpx.ConnectError("refused")):
            result = tool("q")
        text = result if isinstance(result, str) else str(result.content)
        assert "知识库检索服务暂时不可用" in text

    def test_timeout_returns_unavailable_note(self):
        """Timeout -> service unavailable note (fail-open, no raise)."""
        import httpx

        tool = self._make_tool()
        with patch("httpx.get", side_effect=httpx.TimeoutException("t")):
            result = tool("q")
        text = result if isinstance(result, str) else str(result.content)
        assert "知识库检索服务暂时不可用" in text

    def test_unparseable_body_returns_unavailable_note(self):
        """200 with non-JSON body -> service unavailable note (fail-open)."""
        tool = self._make_tool()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.side_effect = ValueError("not json")
        with patch("httpx.get", return_value=mock_resp):
            result = tool("q")
        text = result if isinstance(result, str) else str(result.content)
        assert "知识库检索服务暂时不可用" in text

    def test_empty_query_short_circuits(self):
        """Empty query -> short note, no HTTP call."""
        tool = self._make_tool()
        with patch("httpx.get") as mock_get:
            result = tool("   ")
        assert not mock_get.called
        text = result if isinstance(result, str) else str(result.content)
        assert "检索词为空" in text

    def test_broker_lease_header_sent_when_set(self):
        """When broker_lease is set, X-Tool-Broker-Lease header is sent."""
        tool = self._make_tool(broker_lease="v1.body.sig")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"hits": []}
        with patch("httpx.get", return_value=mock_resp) as mock_get:
            tool("q")
        _, kwargs = mock_get.call_args
        assert kwargs["headers"]["X-Tool-Broker-Lease"] == "v1.body.sig"

    def test_missing_user_id_falls_back_to_system(self):
        """Empty user_id -> X-User-Id: SYSTEM (mirrors inline_knowledge_client)."""
        tool = self._make_tool(user_id="")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"hits": []}
        with patch("httpx.get", return_value=mock_resp) as mock_get:
            tool("q")
        _, kwargs = mock_get.call_args
        assert kwargs["headers"]["X-User-Id"] == "SYSTEM"

    def test_missing_clearance_falls_back_to_l1(self):
        """Empty clearance -> X-User-Clearance: L1 (ki fail-closed default)."""
        tool = self._make_tool(clearance="")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"hits": []}
        with patch("httpx.get", return_value=mock_resp) as mock_get:
            tool("q")
        _, kwargs = mock_get.call_args
        assert kwargs["headers"]["X-User-Clearance"] == "L1"

    def test_custom_k_forwarded(self):
        """k argument is forwarded in the query params."""
        tool = self._make_tool()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"hits": []}
        with patch("httpx.get", return_value=mock_resp) as mock_get:
            tool("q", k=10)
        _, kwargs = mock_get.call_args
        assert kwargs["params"]["k"] == 10


# ---------------------------------------------------------------------------
# install() / hook-wrapping tests
# ---------------------------------------------------------------------------


class TestKnowledgeToolInstall:
    """Tests for the install() wrapper that injects SearchKnowledgeTool."""

    def test_install_appends_tool_when_url_set(self):
        """install() wraps build_toolkit and appends SearchKnowledgeTool when
        KNOWLEDGE_INGEST_URL is set, passing env-derived identity + lease."""
        import agentscope_ext.runtime_worker.hooks as hooks_mod
        from agentscope_ext.knowledge_tool.tool import SearchKnowledgeTool

        original_build = hooks_mod.build_toolkit
        toolkit = _FakeToolkit()
        hooks_mod.build_toolkit = lambda task, tid, env: toolkit

        import agentscope_ext.knowledge_tool as kt_mod

        kt_mod.install()

        env = {
            "KNOWLEDGE_INGEST_URL": "http://fake-ki:8012/api/knowledge/v1",
            "FOUNDATION_TENANT_ID": "tnt-1",
            "FOUNDATION_USER_ID": "usr-1",
            "FOUNDATION_USER_CLEARANCE": "L4",
            "TOOL_BROKER_LEASE": "v1.body.sig",
        }
        result = hooks_mod.build_toolkit({}, "tnt-1", env)

        assert result is toolkit
        assert len(toolkit.tools) == 1
        tool = toolkit.tools[0]
        assert isinstance(tool, SearchKnowledgeTool)
        assert tool._tenant_id == "tnt-1"
        assert tool._user_id == "usr-1"
        assert tool._clearance == "L4"
        assert tool._broker_lease == "v1.body.sig"
        assert tool._base_url == "http://fake-ki:8012/api/knowledge/v1"

        hooks_mod.build_toolkit = original_build

    def test_install_skips_when_url_missing(self, caplog):
        """install() does not append any tool when KNOWLEDGE_INGEST_URL absent."""
        import logging

        import agentscope_ext.runtime_worker.hooks as hooks_mod

        original_build = hooks_mod.build_toolkit
        toolkit = _FakeToolkit()
        hooks_mod.build_toolkit = lambda task, tid, env: toolkit

        import agentscope_ext.knowledge_tool as kt_mod

        kt_mod.install()

        with caplog.at_level(logging.INFO):
            result = hooks_mod.build_toolkit({}, "t1", {})

        assert result is toolkit
        assert len(toolkit.tools) == 0
        assert any("KNOWLEDGE_INGEST_URL is not set" in r.message for r in caplog.records)

        hooks_mod.build_toolkit = original_build

    def test_install_is_idempotent(self):
        """A second install() call is a no-op (no double-wrap)."""
        import agentscope_ext.runtime_worker.hooks as hooks_mod
        from agentscope_ext.knowledge_tool.tool import SearchKnowledgeTool

        original_build = hooks_mod.build_toolkit
        toolkit = _FakeToolkit()
        hooks_mod.build_toolkit = lambda task, tid, env: toolkit

        import agentscope_ext.knowledge_tool as kt_mod

        kt_mod.install()
        kt_mod.install()  # second call -> no-op

        env = {"KNOWLEDGE_INGEST_URL": "http://fake-ki:8012/api/knowledge/v1"}
        hooks_mod.build_toolkit({}, "t1", env)

        assert len(toolkit.tools) == 1
        assert isinstance(toolkit.tools[0], SearchKnowledgeTool)

        hooks_mod.build_toolkit = original_build


# ---------------------------------------------------------------------------
# bootstrap registration
# ---------------------------------------------------------------------------


def test_bootstrap_registers_knowledge_tool():
    """bootstrap._KNOWN maps knowledge_tool -> agentscope_ext.knowledge_tool."""
    from agentscope_ext import bootstrap

    assert bootstrap._KNOWN.get("knowledge_tool") == "agentscope_ext.knowledge_tool"
