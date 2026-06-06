"""Wire-contract tests for ``agentscope_ext.runtime_worker``.

These tests intentionally do **not** import the real AgentScope -- they
stub it via ``sys.modules`` injection so the contract (env validation,
task delivery sources, result.json / events.jsonl shapes, exit codes)
can be exercised in isolation.  The actual ReActAgent reasoning loop is
AgentScope's job to test.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import types
from pathlib import Path
from typing import Any

import pytest

# Ensure the in-tree package is importable when running ``pytest`` from
# the agentscope-ext directory without a prior ``pip install -e .``.
_PKG_ROOT = Path(__file__).resolve().parent.parent
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


REQUIRED_ENV = {
    "FOUNDATION_API_URL": "https://foundation.local",
    "FOUNDATION_API_TOKEN": "tok-123",
    "FOUNDATION_TENANT_ID": "tnt-abc",
    "FOUNDATION_USER_ID": "usr-xyz",
    "LLM_MODEL": "claude-3-5-sonnet-latest",
    "LLM_PROVIDER": "anthropic",
    "LLM_API_KEY": "sk-fake",
}


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """Point the worker's ``WORKSPACE`` constant at a tmp dir for the test."""
    # Reload to pick up a clean module state in case a prior test mutated it.
    import agentscope_ext.runtime_worker.__main__ as worker
    importlib.reload(worker)
    monkeypatch.setattr(worker, "WORKSPACE", tmp_path)
    return tmp_path, worker


@pytest.fixture
def env(monkeypatch):
    """Set the canonical required env vars; tests can override individually."""
    for key in (
        "FOUNDATION_API_URL",
        "FOUNDATION_API_TOKEN",
        "FOUNDATION_TENANT_ID",
        "FOUNDATION_USER_ID",
        "AGENT_TASK_JSON",
        "LLM_MODEL",
        "LLM_PROVIDER",
        "LLM_API_KEY",
        "LLM_BASE_URL",
    ):
        monkeypatch.delenv(key, raising=False)
    for k, v in REQUIRED_ENV.items():
        monkeypatch.setenv(k, v)
    return monkeypatch


# ---------------------------------------------------------------------------
# Fake AgentScope -- installed into sys.modules so ``_run_real`` can import it
# ---------------------------------------------------------------------------


class _FakeTextBlock:
    """Minimal TextBlock stub matching agentscope 2.x API."""

    def __init__(self, *, type: str = "text", text: str = ""):
        self.type = type
        self.text = text


class _FakeMsg:
    def __init__(self, name, content, role):
        self.name = name
        self.content = content
        self.role = role

    def to_dict(self):
        return {"name": self.name, "content": self.content, "role": self.role}

    def get_text_content(self):
        if isinstance(self.content, str):
            return self.content
        return ""


# ---------------------------------------------------------------------------
# Fake events matching agentscope 2.x reply_stream contract
# ---------------------------------------------------------------------------


class TextBlockStartEvent:
    """Fake matching agentscope 2.x — class name must be exactly right."""

    def __init__(self, block_id: str):
        self.block_id = block_id

    def model_dump(self):
        return {"event_type": "TextBlockStartEvent", "block_id": self.block_id}


class TextBlockDeltaEvent:
    def __init__(self, block_id: str, delta: str):
        self.block_id = block_id
        self.delta = delta

    def model_dump(self):
        return {"event_type": "TextBlockDeltaEvent", "block_id": self.block_id, "delta": self.delta}


class ReplyEndEvent:
    def model_dump(self):
        return {"event_type": "ReplyEndEvent"}


class _FakeAgent:
    """Records its construction args; reply_stream yields canned events (AS 2.x)."""

    last_instance: "_FakeAgent | None" = None
    reply_text: str = "ok, did the thing"
    raise_on_call: BaseException | None = None

    def __init__(self, *, name, system_prompt, model, toolkit, state=None, **_kw):
        self.name = name
        self.system_prompt = system_prompt
        self.model = model
        self.toolkit = toolkit
        self.state = state
        _FakeAgent.last_instance = self

    async def reply_stream(self, msg):
        if _FakeAgent.raise_on_call is not None:
            raise _FakeAgent.raise_on_call
        block_id = "blk-1"
        yield TextBlockStartEvent(block_id)
        yield TextBlockDeltaEvent(block_id, _FakeAgent.reply_text)
        yield ReplyEndEvent()


def _install_fake_agentscope(monkeypatch):
    """Mount a minimal stub of the agentscope 2.x API into sys.modules."""
    _FakeAgent.last_instance = None
    _FakeAgent.raise_on_call = None
    _FakeAgent.reply_text = "ok, did the thing"

    agentscope = types.ModuleType("agentscope")
    agent_mod = types.ModuleType("agentscope.agent")
    agent_mod.Agent = _FakeAgent
    message_mod = types.ModuleType("agentscope.message")
    message_mod.Msg = _FakeMsg
    message_mod.TextBlock = _FakeTextBlock

    formatter_mod = types.ModuleType("agentscope.formatter")
    formatter_mod.AnthropicChatFormatter = lambda: "fake-anthropic-formatter"
    formatter_mod.OpenAIChatFormatter = lambda: "fake-openai-formatter"
    formatter_mod.DeepSeekChatFormatter = lambda: "fake-deepseek-formatter"

    model_mod = types.ModuleType("agentscope.model")
    credential_mod = types.ModuleType("agentscope.credential")

    class _FakeCred:
        def __init__(self, **kw):
            self.kw = kw

    class _FakeAnthropic:
        def __init__(self, *, credential, model, stream=False, formatter=None):
            self.credential = credential
            self.model_name = model
            self.kind = "anthropic"

    class _FakeOpenAI:
        def __init__(self, *, credential, model, stream=False, formatter=None):
            self.credential = credential
            self.model_name = model
            self.kind = "openai"

    credential_mod.AnthropicCredential = _FakeCred
    credential_mod.OpenAICredential = _FakeCred
    credential_mod.DeepSeekCredential = _FakeCred
    model_mod.AnthropicChatModel = _FakeAnthropic
    model_mod.OpenAIChatModel = _FakeOpenAI
    model_mod.DeepSeekChatModel = _FakeOpenAI  # same shape for tests

    tool_mod = types.ModuleType("agentscope.tool")

    class _FakeToolkit:
        def __init__(self):
            self.tools: list[Any] = []

        def register_tool_function(self, fn, **_):
            self.tools.append(fn)

    tool_mod.Toolkit = _FakeToolkit

    # Stub agentscope.permission and agentscope.state for the BYPASS path.
    import enum

    class PermissionMode(enum.Enum):
        BYPASS = "bypass"
        CONFIRM = "confirm"

    class PermissionContext:
        def __init__(self, *, mode: PermissionMode):
            self.mode = mode

    class AgentState:
        def __init__(self, *, permission_context: PermissionContext | None = None):
            self.permission_context = permission_context

    permission_mod = types.ModuleType("agentscope.permission")
    permission_mod.PermissionMode = PermissionMode
    permission_mod.PermissionContext = PermissionContext

    state_mod = types.ModuleType("agentscope.state")
    state_mod.AgentState = AgentState

    for name, mod in [
        ("agentscope", agentscope),
        ("agentscope.agent", agent_mod),
        ("agentscope.message", message_mod),
        ("agentscope.formatter", formatter_mod),
        ("agentscope.model", model_mod),
        ("agentscope.credential", credential_mod),
        ("agentscope.tool", tool_mod),
        ("agentscope.permission", permission_mod),
        ("agentscope.state", state_mod),
    ]:
        monkeypatch.setitem(sys.modules, name, mod)


# ---------------------------------------------------------------------------
# Tests -- env + task source contract
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "missing",
    [
        "FOUNDATION_API_URL",
        "FOUNDATION_API_TOKEN",
        "FOUNDATION_TENANT_ID",
        "FOUNDATION_USER_ID",
    ],
)
def test_missing_required_env_exits_1(workspace, env, missing):
    _ws, worker = workspace
    env.delenv(missing, raising=False)

    code = worker.main([])

    assert code == 1
    result = json.loads((_ws / "result.json").read_text())
    assert result["status"] == "FAILED"
    assert result["summary"] == "infra env missing"
    assert missing in (result["error"] or "")


def test_missing_task_exits_2(workspace, env):
    _ws, worker = workspace
    # No AGENT_TASK_JSON, no /workspace/task.json.

    code = worker.main([])

    assert code == 2
    result = json.loads((_ws / "result.json").read_text())
    assert result["status"] == "FAILED"
    assert result["summary"] == "invalid task.json"


def test_malformed_task_json_file_exits_2(workspace, env):
    ws, worker = workspace
    (ws / "task.json").write_text("{not valid json", encoding="utf-8")

    code = worker.main([])

    assert code == 2
    result = json.loads((ws / "result.json").read_text())
    assert result["status"] == "FAILED"
    assert result["summary"] == "invalid task.json"


def test_task_from_env_takes_precedence_over_file(workspace, env, monkeypatch):
    ws, worker = workspace
    _install_fake_agentscope(monkeypatch)

    (ws / "task.json").write_text(
        json.dumps({"task": "from-file"}), encoding="utf-8"
    )
    env.setenv("AGENT_TASK_JSON", json.dumps({"task": "from-env"}))
    _FakeAgent.reply_text = "echoed: from-env"

    code = worker.main([])

    assert code == 0
    result = json.loads((ws / "result.json").read_text())
    assert result["status"] == "OK"
    assert "from-env" in result["summary"]


# ---------------------------------------------------------------------------
# Tests -- result.json / events.jsonl shape
# ---------------------------------------------------------------------------


def test_happy_path_writes_result_and_events(workspace, env, monkeypatch):
    ws, worker = workspace
    _install_fake_agentscope(monkeypatch)

    env.setenv("AGENT_TASK_JSON", json.dumps({"task": "say hi"}))
    _FakeAgent.reply_text = "hi back"

    code = worker.main([])

    assert code == 0
    result = json.loads((ws / "result.json").read_text())
    assert result == {"status": "OK", "summary": "hi back", "error": None}

    # Events come from the fake's reply_stream:
    # TextBlockStartEvent + TextBlockDeltaEvent + ReplyEndEvent = 3 events.
    events_lines = (ws / "events.jsonl").read_text().splitlines()
    assert len(events_lines) == 3
    parsed = [json.loads(line) for line in events_lines]
    event_types = [p.get("event_type") for p in parsed]
    assert "TextBlockStartEvent" in event_types
    assert "TextBlockDeltaEvent" in event_types
    assert "ReplyEndEvent" in event_types
    # The delta carries the reply text.
    delta_event = next(p for p in parsed if p.get("event_type") == "TextBlockDeltaEvent")
    assert delta_event["delta"] == "hi back"


def test_empty_task_text_returns_failed_status_zero(workspace, env, monkeypatch):
    ws, worker = workspace
    _install_fake_agentscope(monkeypatch)
    env.setenv("AGENT_TASK_JSON", json.dumps({"task": "   "}))

    code = worker.main([])

    # exit 0 = task ran to completion; status inside result.json says FAILED.
    assert code == 0
    result = json.loads((ws / "result.json").read_text())
    assert result["status"] == "FAILED"
    assert "empty" in result["summary"]


def test_agent_crash_is_exit_1_with_error_text(workspace, env, monkeypatch):
    ws, worker = workspace
    _install_fake_agentscope(monkeypatch)
    env.setenv("AGENT_TASK_JSON", json.dumps({"task": "boom"}))
    _FakeAgent.raise_on_call = RuntimeError("model unreachable")

    code = worker.main([])

    assert code == 1
    result = json.loads((ws / "result.json").read_text())
    assert result["status"] == "FAILED"
    assert result["summary"] == "worker crashed"
    assert "model unreachable" in (result["error"] or "")


def test_agentscope_import_failure_is_exit_1(workspace, env, monkeypatch):
    ws, worker = workspace
    # Force the agentscope import to fail by setting the module entries to
    # None (Python treats that as ImportError on ``import agentscope.agent``).
    for name in (
        "agentscope",
        "agentscope.agent",
        "agentscope.message",
        "agentscope.formatter",
        "agentscope.model",
        "agentscope.credential",
        "agentscope.tool",
    ):
        monkeypatch.setitem(sys.modules, name, None)

    env.setenv("AGENT_TASK_JSON", json.dumps({"task": "do it"}))

    code = worker.main([])

    assert code == 1
    result = json.loads((ws / "result.json").read_text())
    assert result["status"] == "FAILED"
    assert result["summary"] == "agentscope unavailable"
    assert "ImportError" in (result["error"] or "") or "import" in (result["error"] or "").lower()


# ---------------------------------------------------------------------------
# Tests -- hook contract (override surface for sibling extensions)
# ---------------------------------------------------------------------------


def test_hooks_are_overridable_by_extension(workspace, env, monkeypatch):
    ws, worker = workspace
    _install_fake_agentscope(monkeypatch)
    env.setenv("AGENT_TASK_JSON", json.dumps({"task": "hook check"}))

    from agentscope_ext.runtime_worker import hooks as hooks_mod

    sentinels = {"model": object(), "toolkit": object()}
    attach_calls: list[dict[str, Any]] = []

    monkeypatch.setattr(hooks_mod, "build_model", lambda t, e: sentinels["model"])
    monkeypatch.setattr(
        hooks_mod,
        "build_toolkit",
        lambda t, tid, e: sentinels["toolkit"],
    )
    monkeypatch.setattr(
        hooks_mod,
        "attach_hooks",
        lambda agent, *, events_path, env: attach_calls.append(
            {"agent": agent, "events_path": events_path, "env": dict(env)}
        ),
    )

    code = worker.main([])

    assert code == 0
    inst = _FakeAgent.last_instance
    assert inst is not None
    assert inst.model is sentinels["model"]
    assert inst.toolkit is sentinels["toolkit"]
    assert len(attach_calls) == 1
    assert attach_calls[0]["events_path"] == ws / "events.jsonl"
    assert attach_calls[0]["env"]["FOUNDATION_TENANT_ID"] == "tnt-abc"


def test_default_hooks_pick_provider_from_env(workspace, env, monkeypatch):
    ws, worker = workspace
    _install_fake_agentscope(monkeypatch)
    env.setenv("LLM_PROVIDER", "openai")
    env.setenv("LLM_MODEL", "gpt-4o-mini")
    env.setenv("LLM_BASE_URL", "http://gateway.local/v1")
    env.setenv("AGENT_TASK_JSON", json.dumps({"task": "provider check"}))

    code = worker.main([])

    assert code == 0
    inst = _FakeAgent.last_instance
    assert inst is not None
    assert getattr(inst.model, "kind", None) == "openai"
    assert inst.model.model_name == "gpt-4o-mini"


def test_events_jsonl_exists_after_run(workspace, env, monkeypatch):
    ws, worker = workspace
    _install_fake_agentscope(monkeypatch)
    env.setenv("AGENT_TASK_JSON", json.dumps({"task": "silent run"}))
    _FakeAgent.reply_text = "done"

    code = worker.main([])

    assert code == 0
    events_path = ws / "events.jsonl"
    assert events_path.exists()
    lines = events_path.read_text().splitlines()
    # Fake emits 3 events: start + delta + end.
    assert len(lines) >= 1


def test_unsupported_provider_is_exit_1(workspace, env, monkeypatch):
    ws, worker = workspace
    _install_fake_agentscope(monkeypatch)
    env.setenv("LLM_PROVIDER", "bogus")
    env.setenv("AGENT_TASK_JSON", json.dumps({"task": "x"}))

    code = worker.main([])

    assert code == 1
    result = json.loads((ws / "result.json").read_text())
    assert result["status"] == "FAILED"
    assert result["summary"] == "worker crashed"
    assert "bogus" in (result["error"] or "")


# ---------------------------------------------------------------------------
# Tests -- PART A: prompt file loading + PART B: memory injection
# ---------------------------------------------------------------------------


def test_load_prompt_files_fallback_when_no_s3(workspace, env, monkeypatch):
    """When S3 is not configured, _load_prompt_files returns the default prompt."""
    import agentscope_ext.runtime_worker.__main__ as worker

    env_dict: dict = {}  # no S3 config
    prompt = worker._load_prompt_files(env_dict, {})
    assert "AI 助理" in prompt or "agent" in prompt.lower()


def test_load_prompt_files_merges_files(workspace, env, monkeypatch):
    """S3-backed soul + identity content is merged into the system prompt."""
    import agentscope_ext.runtime_worker.__main__ as worker

    soul_text = "你叫小龙虾"
    identity_text = "你是一位专业分析师"

    # Build a fake s3 client stub.
    class _FakeS3:
        def get_object(self, Bucket, Key):
            if Key.endswith("soul.md"):
                return {"Body": _Body(soul_text)}
            if Key.endswith("identity.md"):
                return {"Body": _Body(identity_text)}
            raise _NoSuchKey()

    class _Body:
        def __init__(self, text):
            self._text = text
        def read(self):
            return self._text.encode("utf-8")

    class _NoSuchKey(Exception):
        pass

    monkeypatch.setattr(worker, "_s3_client", lambda env: _FakeS3())

    env_dict = {
        "S3_BUCKET": "agent-platform",
        "FOUNDATION_TENANT_ID": "tnt_test",
        "FOUNDATION_USER_ID": "usr_test",
    }
    prompt = worker._load_prompt_files(env_dict, {})
    assert soul_text in prompt
    assert identity_text in prompt


def test_memory_injected_into_system_prompt(workspace, env, monkeypatch):
    """task.json[memory] is prepended to the system prompt seen by the agent."""
    ws, worker_mod = workspace
    _install_fake_agentscope(monkeypatch)

    memory_text = "用户上次说他叫张三"
    env.setenv(
        "AGENT_TASK_JSON",
        json.dumps({"task": "我叫什么？", "memory": memory_text}),
    )
    _FakeAgent.reply_text = "你叫张三"

    code = worker_mod.main([])
    assert code == 0

    inst = _FakeAgent.last_instance
    assert inst is not None
    assert memory_text in inst.system_prompt


def test_memory_absent_does_not_break_run(workspace, env, monkeypatch):
    """When task.json has no memory field, the run completes normally."""
    ws, worker_mod = workspace
    _install_fake_agentscope(monkeypatch)

    env.setenv("AGENT_TASK_JSON", json.dumps({"task": "hello"}))
    _FakeAgent.reply_text = "hi"

    code = worker_mod.main([])
    assert code == 0

    inst = _FakeAgent.last_instance
    assert inst is not None
    assert "历史记忆" not in inst.system_prompt


# ---------------------------------------------------------------------------
# Tests -- token streaming via _dispatch + RunStateClient
# ---------------------------------------------------------------------------


def _make_mock_state_client():
    """Return a MagicMock that quacks like RunStateClient."""
    from unittest.mock import MagicMock
    client = MagicMock()
    client.push_token = MagicMock()
    client.push_token_end = MagicMock()
    client.write_step = MagicMock()
    return client


def test_dispatch_calls_push_token_for_each_delta(tmp_path, monkeypatch):
    """_dispatch must call push_token once per TextBlockDeltaEvent delta."""
    import asyncio
    import sys
    import types
    import importlib

    # Install minimal agentscope stubs needed by _dispatch.
    class FakeTextBlockLocal:
        def __init__(self, *, type="text", text=""):
            self.type = type
            self.text = text

    class FakeMsgLocal:
        def __init__(self, name, content, role):
            pass

    # Class names must exactly match what _dispatch checks via type(event).__name__.
    class TextBlockStartEvent:  # noqa: N801
        def __init__(self, block_id):
            self.block_id = block_id
        def model_dump(self):
            return {"event_type": "TextBlockStartEvent", "block_id": self.block_id}

    class TextBlockDeltaEvent:  # noqa: N801
        def __init__(self, block_id, delta):
            self.block_id = block_id
            self.delta = delta
        def model_dump(self):
            return {"event_type": "TextBlockDeltaEvent", "block_id": self.block_id, "delta": self.delta}

    class ReplyEndEvent:  # noqa: N801
        def model_dump(self):
            return {"event_type": "ReplyEndEvent"}

    class MultiDeltaAgent:
        """Emits three deltas across two blocks."""
        async def reply_stream(self, msg):
            yield TextBlockStartEvent("blk-A")
            yield TextBlockDeltaEvent("blk-A", "Hello")
            yield TextBlockDeltaEvent("blk-A", " world")
            yield TextBlockStartEvent("blk-B")
            yield TextBlockDeltaEvent("blk-B", "!")
            yield ReplyEndEvent()

    message_mod = types.ModuleType("agentscope.message")
    message_mod.Msg = FakeMsgLocal
    message_mod.TextBlock = FakeTextBlockLocal
    monkeypatch.setitem(sys.modules, "agentscope.message", message_mod)

    import agentscope_ext.runtime_worker.__main__ as worker
    importlib.reload(worker)

    events_path = tmp_path / "events.jsonl"
    events_path.touch()

    mock_client = _make_mock_state_client()
    agent = MultiDeltaAgent()
    result = asyncio.run(worker._dispatch(agent, "go", events_path, state_client=mock_client))

    # Three deltas must each produce a push_token call, in order.
    from unittest.mock import call
    mock_client.push_token.assert_has_calls([
        call("Hello"),
        call(" world"),
        call("!"),
    ])
    assert mock_client.push_token.call_count == 3

    # push_token_end must be called exactly once, after all deltas.
    mock_client.push_token_end.assert_called_once()

    # Final text must be the joined deltas.
    assert result == "Hello world\n!"


def test_dispatch_push_token_end_called_even_with_no_deltas(tmp_path, monkeypatch):
    """push_token_end must be called even when the agent emits no TextBlockDeltaEvent."""
    import asyncio
    import sys
    import types
    import importlib

    class FakeMsgLocal2:
        def __init__(self, name, content, role):
            pass

    class FakeTextBlockLocal2:
        def __init__(self, *, type="text", text=""):
            pass

    class ReplyEndEvent:  # noqa: N801 — name must match __name__ check
        def model_dump(self):
            return {"event_type": "ReplyEndEvent"}

    class SilentAgent:
        async def reply_stream(self, msg):
            yield ReplyEndEvent()

    message_mod = types.ModuleType("agentscope.message")
    message_mod.Msg = FakeMsgLocal2
    message_mod.TextBlock = FakeTextBlockLocal2
    monkeypatch.setitem(sys.modules, "agentscope.message", message_mod)

    import agentscope_ext.runtime_worker.__main__ as worker
    importlib.reload(worker)

    events_path = tmp_path / "events.jsonl"
    events_path.touch()

    mock_client = _make_mock_state_client()
    result = asyncio.run(worker._dispatch(SilentAgent(), "ping", events_path, state_client=mock_client))

    mock_client.push_token.assert_not_called()
    mock_client.push_token_end.assert_called_once()
    assert result == ""


def test_dispatch_no_state_client_does_not_call_push_token(tmp_path, monkeypatch):
    """When state_client is None _dispatch must not call any streaming methods."""
    import asyncio
    import sys
    import types
    import importlib

    class FakeMsgLocal3:
        def __init__(self, name, content, role):
            pass

    class FakeTextBlockLocal3:
        def __init__(self, *, type="text", text=""):
            pass

    class TextBlockDeltaEvent:  # noqa: N801 — name must match
        def __init__(self, block_id, delta):
            self.block_id = block_id
            self.delta = delta
        def model_dump(self):
            return {"event_type": "TextBlockDeltaEvent", "block_id": self.block_id, "delta": self.delta}

    class AgentWithDelta:
        async def reply_stream(self, msg):
            yield TextBlockDeltaEvent("blk", "data")

    message_mod = types.ModuleType("agentscope.message")
    message_mod.Msg = FakeMsgLocal3
    message_mod.TextBlock = FakeTextBlockLocal3
    monkeypatch.setitem(sys.modules, "agentscope.message", message_mod)

    import agentscope_ext.runtime_worker.__main__ as worker
    importlib.reload(worker)

    events_path = tmp_path / "events.jsonl"
    events_path.touch()

    # Must not raise even with state_client=None.
    result = asyncio.run(worker._dispatch(AgentWithDelta(), "x", events_path, state_client=None))
    assert result == "data"


def test_dispatch_result_json_unaffected_by_streaming(workspace, env, monkeypatch):
    """Enabling token streaming must not change result.json output."""
    from unittest.mock import MagicMock
    ws, worker_mod = workspace
    _install_fake_agentscope(monkeypatch)

    # Install a fake RunStateClient that records push calls.
    import agentscope_ext.runtime_worker.redis_state as rs_mod
    importlib.reload(rs_mod)

    push_log: list[str] = []
    end_log: list[bool] = []

    class _TrackingClient:
        def read_checkpoint(self): return None
        def write_step(self, **_kw): pass
        def mark_completed(self): pass
        def mark_failed(self): pass
        def push_token(self, delta): push_log.append(delta)
        def push_token_end(self): end_log.append(True)

    monkeypatch.setattr(rs_mod, "from_env", lambda: _TrackingClient())

    env.setenv("AGENT_TASK_JSON", json.dumps({"task": "stream me"}))
    _FakeAgent.reply_text = "streamed reply"

    code = worker_mod.main([])

    assert code == 0
    result = json.loads((ws / "result.json").read_text())
    assert result == {"status": "OK", "summary": "streamed reply", "error": None}

    # Streaming side-effects happened.
    assert push_log == ["streamed reply"]
    assert end_log == [True]


# ---------------------------------------------------------------------------
# Tests -- BYPASS permission context is injected into Agent
# ---------------------------------------------------------------------------


def test_agent_constructed_with_bypass_permission_state(workspace, env, monkeypatch):
    """_run_real must pass an AgentState with PermissionMode.BYPASS to Agent
    so headless tool calls are auto-approved without user confirmation."""
    ws, worker_mod = workspace
    _install_fake_agentscope(monkeypatch)

    env.setenv("AGENT_TASK_JSON", json.dumps({"task": "do something"}))
    _FakeAgent.reply_text = "done"

    code = worker_mod.main([])
    assert code == 0

    inst = _FakeAgent.last_instance
    assert inst is not None, "Agent was never constructed"

    # state must not be None — BYPASS path succeeded.
    assert inst.state is not None, (
        "Agent.state is None; BYPASS permission was not injected. "
        "Tool calls will require user confirmation in the headless sandbox."
    )

    ctx = inst.state.permission_context
    assert ctx is not None, "state.permission_context is None"

    # Import the fake PermissionMode that was installed by _install_fake_agentscope.
    from agentscope.permission import PermissionMode  # type: ignore[import]

    assert ctx.mode == PermissionMode.BYPASS, (
        f"Expected PermissionMode.BYPASS but got {ctx.mode!r}"
    )


def test_agent_bypass_permission_fallback_when_permission_module_absent(
    workspace, env, monkeypatch
):
    """If agentscope.permission / agentscope.state are missing the worker must
    still complete normally (failure-open) without raising."""
    ws, worker_mod = workspace
    _install_fake_agentscope(monkeypatch)

    # Remove the permission/state stubs to simulate an older AgentScope version.
    monkeypatch.setitem(sys.modules, "agentscope.permission", None)
    monkeypatch.setitem(sys.modules, "agentscope.state", None)

    env.setenv("AGENT_TASK_JSON", json.dumps({"task": "do something fallback"}))
    _FakeAgent.reply_text = "fallback ok"

    code = worker_mod.main([])
    assert code == 0, "Worker must not crash when permission module is absent"

    result = json.loads((ws / "result.json").read_text())
    assert result["status"] == "OK"

    # state should be None (fell back gracefully).
    inst = _FakeAgent.last_instance
    assert inst is not None
    assert inst.state is None, (
        "Expected state=None when permission module unavailable, "
        f"got {inst.state!r}"
    )


# ---------------------------------------------------------------------------
# Tests -- tool call/result streaming via _dispatch
# ---------------------------------------------------------------------------


def _make_tool_mock_state_client():
    """Return a MagicMock that quacks like RunStateClient for tool streaming tests."""
    from unittest.mock import MagicMock

    client = MagicMock()
    client.push_token = MagicMock()
    client.push_token_end = MagicMock()
    client.push_tool_call = MagicMock()
    client.push_tool_result = MagicMock()
    client.push_thinking = MagicMock()
    client.write_step = MagicMock()
    return client


def _make_agentscope_message_stub(monkeypatch):
    """Install a minimal agentscope.message stub needed by _dispatch."""

    class _FakeTB:
        def __init__(self, *, type="text", text=""):
            self.type = type
            self.text = text

    class _FakeMsg:
        def __init__(self, name, content, role):
            pass

    msg_mod = types.ModuleType("agentscope.message")
    msg_mod.Msg = _FakeMsg
    msg_mod.TextBlock = _FakeTB
    monkeypatch.setitem(sys.modules, "agentscope.message", msg_mod)


def test_dispatch_tool_call_accumulates_args_and_pushes_on_end(tmp_path, monkeypatch):
    """_dispatch must accumulate ToolCallDelta args, then call push_tool_call on ToolCallEnd."""
    _make_agentscope_message_stub(monkeypatch)

    class ToolCallStartEvent:
        def __init__(self, tool_call_id, tool_call_name):
            self.tool_call_id = tool_call_id
            self.tool_call_name = tool_call_name
        def model_dump(self):
            return {"event_type": "ToolCallStartEvent", "tool_call_id": self.tool_call_id}

    class ToolCallDeltaEvent:
        def __init__(self, tool_call_id, delta):
            self.tool_call_id = tool_call_id
            self.delta = delta
        def model_dump(self):
            return {"event_type": "ToolCallDeltaEvent", "tool_call_id": self.tool_call_id}

    class ToolCallEndEvent:
        def __init__(self, tool_call_id):
            self.tool_call_id = tool_call_id
        def model_dump(self):
            return {"event_type": "ToolCallEndEvent", "tool_call_id": self.tool_call_id}

    class ReplyEndEvent:
        def model_dump(self):
            return {"event_type": "ReplyEndEvent"}

    class ToolCallAgent:
        async def reply_stream(self, msg):
            yield ToolCallStartEvent("tc-1", "Read")
            yield ToolCallDeltaEvent("tc-1", '{"file": "src/')
            yield ToolCallDeltaEvent("tc-1", 'auth.ts", "limit": 100}')
            yield ToolCallEndEvent("tc-1")
            yield ReplyEndEvent()

    import asyncio
    import importlib
    import agentscope_ext.runtime_worker.__main__ as worker
    importlib.reload(worker)

    events_path = tmp_path / "events.jsonl"
    events_path.touch()
    mock_client = _make_tool_mock_state_client()

    asyncio.run(worker._dispatch(ToolCallAgent(), "task", events_path, state_client=mock_client))

    # push_tool_call must be called exactly once with the fully accumulated args.
    mock_client.push_tool_call.assert_called_once_with(
        "tc-1",
        "Read",
        '{"file": "src/auth.ts", "limit": 100}',
    )
    # push_tool_result must NOT be called (no result events).
    mock_client.push_tool_result.assert_not_called()


def test_dispatch_tool_result_accumulates_text_and_pushes_on_end(tmp_path, monkeypatch):
    """_dispatch must accumulate ToolResultTextDelta text, then call push_tool_result on ToolResultEnd."""
    _make_agentscope_message_stub(monkeypatch)

    class ToolResultStartEvent:
        def __init__(self, tool_call_id, tool_call_name):
            self.tool_call_id = tool_call_id
            self.tool_call_name = tool_call_name
        def model_dump(self):
            return {"event_type": "ToolResultStartEvent"}

    class ToolResultTextDeltaEvent:
        def __init__(self, tool_call_id, delta):
            self.tool_call_id = tool_call_id
            self.delta = delta
        def model_dump(self):
            return {"event_type": "ToolResultTextDeltaEvent"}

    class ToolResultEndEvent:
        def __init__(self, tool_call_id, state):
            self.tool_call_id = tool_call_id
            self.state = state
        def model_dump(self):
            return {"event_type": "ToolResultEndEvent"}

    class ReplyEndEvent:
        def model_dump(self):
            return {"event_type": "ReplyEndEvent"}

    class ToolResultAgent:
        async def reply_stream(self, msg):
            yield ToolResultStartEvent("tc-2", "Read")
            yield ToolResultTextDeltaEvent("tc-2", "line 1\n")
            yield ToolResultTextDeltaEvent("tc-2", "line 2\n")
            yield ToolResultEndEvent("tc-2", "success")
            yield ReplyEndEvent()

    import asyncio
    import importlib
    import agentscope_ext.runtime_worker.__main__ as worker
    importlib.reload(worker)

    events_path = tmp_path / "events.jsonl"
    events_path.touch()
    mock_client = _make_tool_mock_state_client()

    asyncio.run(worker._dispatch(ToolResultAgent(), "task", events_path, state_client=mock_client))

    mock_client.push_tool_result.assert_called_once_with(
        "tc-2",
        "Read",
        "line 1\nline 2\n",
        "success",
    )
    mock_client.push_tool_call.assert_not_called()


def test_dispatch_full_tool_call_and_result_sequence(tmp_path, monkeypatch):
    """_dispatch handles a full ToolCallStart→Delta→End→ResultStart→ResultDelta→ResultEnd cycle."""
    _make_agentscope_message_stub(monkeypatch)

    class ToolCallStartEvent:
        def __init__(self, tool_call_id, tool_call_name):
            self.tool_call_id = tool_call_id
            self.tool_call_name = tool_call_name
        def model_dump(self):
            return {"event_type": "ToolCallStartEvent"}

    class ToolCallDeltaEvent:
        def __init__(self, tool_call_id, delta):
            self.tool_call_id = tool_call_id
            self.delta = delta
        def model_dump(self):
            return {"event_type": "ToolCallDeltaEvent"}

    class ToolCallEndEvent:
        def __init__(self, tool_call_id):
            self.tool_call_id = tool_call_id
        def model_dump(self):
            return {"event_type": "ToolCallEndEvent"}

    class ToolResultStartEvent:
        def __init__(self, tool_call_id, tool_call_name):
            self.tool_call_id = tool_call_id
            self.tool_call_name = tool_call_name
        def model_dump(self):
            return {"event_type": "ToolResultStartEvent"}

    class ToolResultTextDeltaEvent:
        def __init__(self, tool_call_id, delta):
            self.tool_call_id = tool_call_id
            self.delta = delta
        def model_dump(self):
            return {"event_type": "ToolResultTextDeltaEvent"}

    class ToolResultEndEvent:
        def __init__(self, tool_call_id, state):
            self.tool_call_id = tool_call_id
            self.state = state
        def model_dump(self):
            return {"event_type": "ToolResultEndEvent"}

    class TextBlockStartEvent:
        def __init__(self, block_id):
            self.block_id = block_id
        def model_dump(self):
            return {"event_type": "TextBlockStartEvent", "block_id": self.block_id}

    class TextBlockDeltaEvent:
        def __init__(self, block_id, delta):
            self.block_id = block_id
            self.delta = delta
        def model_dump(self):
            return {"event_type": "TextBlockDeltaEvent", "block_id": self.block_id, "delta": self.delta}

    class ReplyEndEvent:
        def model_dump(self):
            return {"event_type": "ReplyEndEvent"}

    class FullCycleAgent:
        async def reply_stream(self, msg):
            # Tool use first
            yield ToolCallStartEvent("tc-A", "Grep")
            yield ToolCallDeltaEvent("tc-A", '{"pattern": "def ')
            yield ToolCallDeltaEvent("tc-A", 'main"}')
            yield ToolCallEndEvent("tc-A")
            yield ToolResultStartEvent("tc-A", "Grep")
            yield ToolResultTextDeltaEvent("tc-A", "Found in ")
            yield ToolResultTextDeltaEvent("tc-A", "main.py:5")
            yield ToolResultEndEvent("tc-A", "success")
            # Then text reply
            yield TextBlockStartEvent("blk-1")
            yield TextBlockDeltaEvent("blk-1", "Done.")
            yield ReplyEndEvent()

    import asyncio
    import importlib
    from unittest.mock import call as mcall
    import agentscope_ext.runtime_worker.__main__ as worker
    importlib.reload(worker)

    events_path = tmp_path / "events.jsonl"
    events_path.touch()
    mock_client = _make_tool_mock_state_client()

    result = asyncio.run(
        worker._dispatch(FullCycleAgent(), "task", events_path, state_client=mock_client)
    )

    # Tool call: args fully accumulated
    mock_client.push_tool_call.assert_called_once_with(
        "tc-A", "Grep", '{"pattern": "def main"}'
    )
    # Tool result: text fully accumulated, state preserved
    mock_client.push_tool_result.assert_called_once_with(
        "tc-A", "Grep", "Found in main.py:5", "success"
    )
    # Token streaming still works
    mock_client.push_token.assert_called_once_with("Done.")
    mock_client.push_token_end.assert_called_once()

    # Call order: push_tool_call before push_tool_result before push_token.
    all_calls = mock_client.method_calls
    call_names = [c[0] for c in all_calls if c[0] in ("push_tool_call", "push_tool_result", "push_token", "push_token_end")]
    assert call_names.index("push_tool_call") < call_names.index("push_tool_result")
    assert call_names.index("push_tool_result") < call_names.index("push_token")

    assert result == "Done."


def test_dispatch_thinking_event_pushes_thinking(tmp_path, monkeypatch):
    """ThinkEvent must call push_thinking with the thought text."""
    _make_agentscope_message_stub(monkeypatch)

    class ThinkEvent:
        def __init__(self, thought):
            self.thought = thought
        def model_dump(self):
            return {"event_type": "ThinkEvent", "thought": self.thought}

    class ReplyEndEvent:
        def model_dump(self):
            return {"event_type": "ReplyEndEvent"}

    class ThinkingAgent:
        async def reply_stream(self, msg):
            yield ThinkEvent("I should read the auth file")
            yield ReplyEndEvent()

    import asyncio
    import importlib
    import agentscope_ext.runtime_worker.__main__ as worker
    importlib.reload(worker)

    events_path = tmp_path / "events.jsonl"
    events_path.touch()
    mock_client = _make_tool_mock_state_client()

    asyncio.run(worker._dispatch(ThinkingAgent(), "task", events_path, state_client=mock_client))

    mock_client.push_thinking.assert_called_once_with("I should read the auth file")
