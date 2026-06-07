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


class ModelCallEndEvent:
    """Fake ModelCallEndEvent with token usage fields."""

    def __init__(self, input_tokens: int = 0, output_tokens: int = 0):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens

    def model_dump(self):
        return {
            "event_type": "ModelCallEndEvent",
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
        }


class ToolCallStartEvent:
    def __init__(self, tool_call_id: str, tool_call_name: str):
        self.tool_call_id = tool_call_id
        self.tool_call_name = tool_call_name

    def model_dump(self):
        return {
            "event_type": "ToolCallStartEvent",
            "tool_call_id": self.tool_call_id,
            "tool_call_name": self.tool_call_name,
        }


class ToolCallDeltaEvent:
    def __init__(self, tool_call_id: str, delta: str):
        self.tool_call_id = tool_call_id
        self.delta = delta

    def model_dump(self):
        return {
            "event_type": "ToolCallDeltaEvent",
            "tool_call_id": self.tool_call_id,
            "delta": self.delta,
        }


class ToolCallEndEvent:
    def __init__(self, tool_call_id: str):
        self.tool_call_id = tool_call_id

    def model_dump(self):
        return {
            "event_type": "ToolCallEndEvent",
            "tool_call_id": self.tool_call_id,
        }


class ToolResultStartEvent:
    def __init__(self, tool_call_id: str, tool_call_name: str):
        self.tool_call_id = tool_call_id
        self.tool_call_name = tool_call_name

    def model_dump(self):
        return {
            "event_type": "ToolResultStartEvent",
            "tool_call_id": self.tool_call_id,
            "tool_call_name": self.tool_call_name,
        }


class ToolResultTextDeltaEvent:
    def __init__(self, tool_call_id: str, delta: str):
        self.tool_call_id = tool_call_id
        self.delta = delta

    def model_dump(self):
        return {
            "event_type": "ToolResultTextDeltaEvent",
            "tool_call_id": self.tool_call_id,
            "delta": self.delta,
        }


class ToolResultEndEvent:
    def __init__(self, tool_call_id: str, state: str):
        self.tool_call_id = tool_call_id
        self.state = state

    def model_dump(self):
        return {
            "event_type": "ToolResultEndEvent",
            "tool_call_id": self.tool_call_id,
            "state": self.state,
        }


class _FakeRedisClient:
    """Minimal redis.Redis client stub for worker integration tests."""

    def __init__(self) -> None:
        self.xadd_calls: list[tuple[str, dict[str, str], dict[str, Any]]] = []
        self.set_calls: list[tuple[str, str, dict[str, Any]]] = []
        self.expire_calls: list[tuple[str, int]] = []
        self.get_value: str | None = None

    def ping(self) -> bool:
        return True

    def get(self, key: str) -> str | None:
        del key
        return self.get_value

    def set(self, key: str, value: str, **kwargs: Any) -> None:
        self.set_calls.append((key, value, kwargs))

    def expire(self, key: str, ttl: int) -> None:
        self.expire_calls.append((key, ttl))

    def xadd(self, key: str, fields: dict[str, str], **kwargs: Any) -> None:
        self.xadd_calls.append((key, fields, kwargs))


def _install_fake_redis(monkeypatch) -> _FakeRedisClient:
    client = _FakeRedisClient()

    class _FakeRedis:
        @staticmethod
        def from_url(*_args: Any, **_kwargs: Any) -> _FakeRedisClient:
            return client

    redis_mod = types.ModuleType("redis")
    redis_mod.Redis = _FakeRedis
    monkeypatch.setitem(sys.modules, "redis", redis_mod)
    return client


class _FakeAgent:
    """Records its construction args; reply_stream yields canned events (AS 2.x)."""

    last_instance: "_FakeAgent | None" = None
    reply_text: str = "ok, did the thing"
    raise_on_call: BaseException | None = None

    def __init__(self, *, name, system_prompt, model, toolkit, **_kw):
        self.name = name
        self.system_prompt = system_prompt
        self.model = model
        self.toolkit = toolkit
        _FakeAgent.last_instance = self

    async def reply_stream(self, msg):
        if _FakeAgent.raise_on_call is not None:
            raise _FakeAgent.raise_on_call
        block_id = "blk-1"
        yield TextBlockStartEvent(block_id)
        yield TextBlockDeltaEvent(block_id, _FakeAgent.reply_text)
        yield ReplyEndEvent()


class _FakeAgentWithToolStream(_FakeAgent):
    """Emits tool call/result events interleaved with assistant text."""

    async def reply_stream(self, msg):
        del msg
        block_id = "blk-tool"
        yield ToolCallStartEvent("tc-1", "Bash")
        yield ToolCallDeltaEvent("tc-1", '{"cmd":')
        yield ToolCallDeltaEvent("tc-1", '"pwd"}')
        yield ToolCallEndEvent("tc-1")
        yield ToolResultStartEvent("tc-1", "Bash")
        yield ToolResultTextDeltaEvent("tc-1", "/workspace")
        yield ToolResultEndEvent("tc-1", "success")
        yield TextBlockStartEvent(block_id)
        yield TextBlockDeltaEvent(block_id, "done")
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
        def __init__(self, tools=None, **_kw):
            self.tools: list[Any] = list(tools or [])
            self.tool_groups = [types.SimpleNamespace(mcps=[])]

        def register_tool_function(self, fn, **_):
            self.tools.append(fn)

    # Minimal stubs for the built-in planning tools so hooks.build_toolkit
    # can call Toolkit(tools=[TaskCreate(), TaskUpdate(), TaskList()]).
    class _FakeTaskCreate:
        pass

    class _FakeTaskUpdate:
        pass

    class _FakeTaskList:
        pass

    tool_mod.Toolkit = _FakeToolkit
    tool_mod.TaskCreate = _FakeTaskCreate
    tool_mod.TaskUpdate = _FakeTaskUpdate
    tool_mod.TaskList = _FakeTaskList

    for name, mod in [
        ("agentscope", agentscope),
        ("agentscope.agent", agent_mod),
        ("agentscope.message", message_mod),
        ("agentscope.formatter", formatter_mod),
        ("agentscope.model", model_mod),
        ("agentscope.credential", credential_mod),
        ("agentscope.tool", tool_mod),
    ]:
        monkeypatch.setitem(sys.modules, name, mod)


def _install_fake_agentscope_with_tool_stream(monkeypatch):
    _install_fake_agentscope(monkeypatch)
    agent_mod = sys.modules["agentscope.agent"]
    agent_mod.Agent = _FakeAgentWithToolStream


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
    assert result["status"] == "OK"
    assert result["summary"] == "hi back"
    assert result["error"] is None
    # usage is always present (may be all-zeros when no ModelCallEndEvent fired).
    assert "usage" in result
    assert isinstance(result["usage"], dict)

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


def test_worker_pushes_reply_deltas_to_redis_token_stream(workspace, env, monkeypatch):
    ws, worker = workspace
    _install_fake_agentscope(monkeypatch)
    redis_client = _install_fake_redis(monkeypatch)
    env.setenv("REDIS_URL", "redis://host:6379/0")
    env.setenv("RUN_ID", "run_stream")
    env.setenv("AGENT_TASK_JSON", json.dumps({"task": "say hi"}))
    _FakeAgent.reply_text = "hi back"

    code = worker.main([])

    assert code == 0
    assert json.loads((ws / "result.json").read_text())["summary"] == "hi back"

    token_entries = [
        fields for key, fields, _kwargs in redis_client.xadd_calls
        if key == "agent:run:run_stream:tokens"
    ]
    assert token_entries == [
        {"type": "delta", "text": "hi back"},
        {"type": "end", "text": ""},
    ]


def test_worker_pushes_tool_events_to_redis_token_stream(workspace, env, monkeypatch):
    ws, worker = workspace
    _install_fake_agentscope_with_tool_stream(monkeypatch)
    redis_client = _install_fake_redis(monkeypatch)
    env.setenv("REDIS_URL", "redis://host:6379/0")
    env.setenv("RUN_ID", "run_tools")
    env.setenv("AGENT_TASK_JSON", json.dumps({"task": "use a tool"}))

    code = worker.main([])

    assert code == 0
    assert json.loads((ws / "result.json").read_text())["summary"] == "done"

    token_entries = [
        fields for key, fields, _kwargs in redis_client.xadd_calls
        if key == "agent:run:run_tools:tokens"
    ]
    assert token_entries == [
        {
            "type": "tool_call",
            "tool_call_id": "tc-1",
            "name": "Bash",
            "args": '{"cmd":"pwd"}',
        },
        {
            "type": "tool_result",
            "tool_call_id": "tc-1",
            "name": "Bash",
            "result": "/workspace",
            "state": "success",
        },
        {"type": "delta", "text": "done"},
        {"type": "end", "text": ""},
    ]


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
# Tests -- token accumulation via ModelCallEndEvent
# ---------------------------------------------------------------------------


class _FakeAgentWithTokens:
    """Like _FakeAgent but emits ModelCallEndEvent(s) to test token accumulation."""

    last_instance: "_FakeAgentWithTokens | None" = None
    # Each entry is (input_tokens, output_tokens) for one model call.
    model_call_usages: list[tuple[int, int]] = []
    reply_text: str = "done"

    def __init__(self, *, name, system_prompt, model, toolkit, **_kw):
        self.name = name
        self.system_prompt = system_prompt
        self.model = model
        self.toolkit = toolkit
        _FakeAgentWithTokens.last_instance = self

    async def reply_stream(self, msg):
        block_id = "blk-t"
        yield TextBlockStartEvent(block_id)
        for in_tok, out_tok in _FakeAgentWithTokens.model_call_usages:
            yield ModelCallEndEvent(input_tokens=in_tok, output_tokens=out_tok)
        yield TextBlockDeltaEvent(block_id, _FakeAgentWithTokens.reply_text)
        yield ReplyEndEvent()


def _install_fake_agentscope_with_tokens(monkeypatch):
    """Like _install_fake_agentscope but uses _FakeAgentWithTokens."""
    _FakeAgentWithTokens.last_instance = None
    _FakeAgentWithTokens.model_call_usages = []
    _FakeAgentWithTokens.reply_text = "done"

    import agentscope_ext.runtime_worker.__main__ as worker_mod
    import agentscope_ext.runtime_worker.hooks as hooks_mod
    import agentscope_ext.runtime_worker.redis_state as redis_mod

    # Reuse the agentscope stubs from _install_fake_agentscope but swap Agent.
    import types as _types

    agentscope = _types.ModuleType("agentscope")
    agent_mod = _types.ModuleType("agentscope.agent")
    agent_mod.Agent = _FakeAgentWithTokens
    message_mod = _types.ModuleType("agentscope.message")
    message_mod.Msg = _FakeMsg
    message_mod.TextBlock = _FakeTextBlock

    formatter_mod = _types.ModuleType("agentscope.formatter")
    formatter_mod.AnthropicChatFormatter = lambda: "fake-anthropic-formatter"
    formatter_mod.OpenAIChatFormatter = lambda: "fake-openai-formatter"
    formatter_mod.DeepSeekChatFormatter = lambda: "fake-deepseek-formatter"

    model_mod = _types.ModuleType("agentscope.model")
    credential_mod = _types.ModuleType("agentscope.credential")

    class _FakeCred:
        def __init__(self, **kw):
            self.kw = kw

    class _FakeAnthropic:
        def __init__(self, *, credential, model, stream=False, formatter=None):
            self.credential = credential
            self.model_name = model
            self.kind = "anthropic"

    credential_mod.AnthropicCredential = _FakeCred
    credential_mod.OpenAICredential = _FakeCred
    credential_mod.DeepSeekCredential = _FakeCred
    model_mod.AnthropicChatModel = _FakeAnthropic
    model_mod.OpenAIChatModel = _FakeAnthropic
    model_mod.DeepSeekChatModel = _FakeAnthropic

    tool_mod = _types.ModuleType("agentscope.tool")

    class _FakeToolkit:
        def __init__(self, tools=None, **_kw):
            self.tools: list = list(tools or [])
            self.tool_groups = [_types.SimpleNamespace(mcps=[])]

        def register_tool_function(self, fn, **_):
            self.tools.append(fn)

    class _FakeTaskCreate:
        pass

    class _FakeTaskUpdate:
        pass

    class _FakeTaskList:
        pass

    tool_mod.Toolkit = _FakeToolkit
    tool_mod.TaskCreate = _FakeTaskCreate
    tool_mod.TaskUpdate = _FakeTaskUpdate
    tool_mod.TaskList = _FakeTaskList

    for name, mod in [
        ("agentscope", agentscope),
        ("agentscope.agent", agent_mod),
        ("agentscope.message", message_mod),
        ("agentscope.formatter", formatter_mod),
        ("agentscope.model", model_mod),
        ("agentscope.credential", credential_mod),
        ("agentscope.tool", tool_mod),
    ]:
        monkeypatch.setitem(sys.modules, name, mod)


def test_token_counts_accumulated_from_model_call_end_events(workspace, env, monkeypatch):
    """ModelCallEndEvent tokens are accumulated and written to result.json usage."""
    ws, worker_mod = workspace
    _install_fake_agentscope_with_tokens(monkeypatch)

    _FakeAgentWithTokens.model_call_usages = [(100, 50), (200, 80)]
    _FakeAgentWithTokens.reply_text = "summary"
    env.setenv("AGENT_TASK_JSON", json.dumps({"task": "count tokens"}))

    code = worker_mod.main([])

    assert code == 0
    result = json.loads((ws / "result.json").read_text())
    assert result["status"] == "OK"
    usage = result["usage"]
    assert usage["input_tokens"] == 300   # 100 + 200
    assert usage["output_tokens"] == 130  # 50 + 80
    assert usage["total_tokens"] == 430   # 300 + 130


def test_token_counts_zero_when_no_model_call_end_events(workspace, env, monkeypatch):
    """When no ModelCallEndEvent is emitted, usage is all zeros."""
    ws, worker_mod = workspace
    _install_fake_agentscope(monkeypatch)

    _FakeAgent.reply_text = "no tokens"
    env.setenv("AGENT_TASK_JSON", json.dumps({"task": "silent"}))

    code = worker_mod.main([])

    assert code == 0
    result = json.loads((ws / "result.json").read_text())
    usage = result["usage"]
    assert usage["input_tokens"] == 0
    assert usage["output_tokens"] == 0
    assert usage["total_tokens"] == 0


# ---------------------------------------------------------------------------
# Tests -- plan_steps extraction + result.json persistence
# ---------------------------------------------------------------------------


class _FakeTask:
    """Minimal Task stub for plan_steps extraction tests."""

    def __init__(self, subject: str, state: str):
        self.subject = subject
        self.state = state


class _FakeTasksContext:
    def __init__(self, tasks):
        self.tasks = tasks


class _ToolCallEndEvent:
    """Fake ToolCallEndEvent — type().__name__ must equal 'ToolCallEndEvent'."""

    def model_dump(self):
        return {"event_type": "ToolCallEndEvent"}


class _FakeAgentWithTasks:
    """Agent stub whose state exposes tasks_context for plan extraction."""

    last_instance: "_FakeAgentWithTasks | None" = None
    reply_text: str = "done"
    tasks: list = []  # set per-test

    def __init__(self, *, name, system_prompt, model, toolkit, **_kw):
        self.name = name
        self.system_prompt = system_prompt
        self.model = model
        self.toolkit = toolkit
        # Attach a state object with tasks_context.
        self.state = types.SimpleNamespace(
            tasks_context=_FakeTasksContext(list(_FakeAgentWithTasks.tasks))
        )
        _FakeAgentWithTasks.last_instance = self

    async def reply_stream(self, msg):
        block_id = "blk-plan"
        yield TextBlockStartEvent(block_id)
        # Emit a ToolCallEndEvent so _dispatch re-reads tasks.
        yield _ToolCallEndEvent()
        yield TextBlockDeltaEvent(block_id, _FakeAgentWithTasks.reply_text)
        yield ReplyEndEvent()


def _install_fake_agentscope_with_tasks(monkeypatch):
    """Install stubs identical to _install_fake_agentscope but using _FakeAgentWithTasks."""
    _FakeAgentWithTasks.last_instance = None
    _FakeAgentWithTasks.tasks = []
    _FakeAgentWithTasks.reply_text = "done"

    _types = types  # local alias

    agentscope = _types.ModuleType("agentscope")
    agent_mod = _types.ModuleType("agentscope.agent")
    agent_mod.Agent = _FakeAgentWithTasks
    message_mod = _types.ModuleType("agentscope.message")
    message_mod.Msg = _FakeMsg
    message_mod.TextBlock = _FakeTextBlock

    formatter_mod = _types.ModuleType("agentscope.formatter")
    formatter_mod.AnthropicChatFormatter = lambda: "fake-fmt"
    formatter_mod.OpenAIChatFormatter = lambda: "fake-fmt"
    formatter_mod.DeepSeekChatFormatter = lambda: "fake-fmt"

    model_mod = _types.ModuleType("agentscope.model")
    credential_mod = _types.ModuleType("agentscope.credential")

    class _FakeCred:
        def __init__(self, **kw):
            pass

    class _FakeAnthropic:
        def __init__(self, *, credential, model, stream=False, formatter=None):
            self.model_name = model
            self.kind = "anthropic"

    credential_mod.AnthropicCredential = _FakeCred
    credential_mod.OpenAICredential = _FakeCred
    credential_mod.DeepSeekCredential = _FakeCred
    model_mod.AnthropicChatModel = _FakeAnthropic
    model_mod.OpenAIChatModel = _FakeAnthropic
    model_mod.DeepSeekChatModel = _FakeAnthropic

    tool_mod = _types.ModuleType("agentscope.tool")

    class _FakeToolkit:
        def __init__(self, tools=None):
            self.tools: list = tools or []
            self.tool_groups = [_types.SimpleNamespace(mcps=[])]

    class _FakeTaskCreate:
        pass

    class _FakeTaskUpdate:
        pass

    class _FakeTaskList:
        pass

    tool_mod.Toolkit = _FakeToolkit
    tool_mod.TaskCreate = _FakeTaskCreate
    tool_mod.TaskUpdate = _FakeTaskUpdate
    tool_mod.TaskList = _FakeTaskList

    for name, mod in [
        ("agentscope", agentscope),
        ("agentscope.agent", agent_mod),
        ("agentscope.message", message_mod),
        ("agentscope.formatter", formatter_mod),
        ("agentscope.model", model_mod),
        ("agentscope.credential", credential_mod),
        ("agentscope.tool", tool_mod),
    ]:
        monkeypatch.setitem(sys.modules, name, mod)


def test_plan_steps_empty_when_no_tasks(workspace, env, monkeypatch):
    """result.json always carries plan_steps key; it is [] when agent made no tasks."""
    ws, worker_mod = workspace
    _install_fake_agentscope(monkeypatch)

    _FakeAgent.reply_text = "no tasks"
    env.setenv("AGENT_TASK_JSON", json.dumps({"task": "simple"}))

    code = worker_mod.main([])

    assert code == 0
    result = json.loads((ws / "result.json").read_text())
    assert "plan_steps" in result
    assert result["plan_steps"] == []


def test_plan_steps_extracted_from_agent_tasks(workspace, env, monkeypatch):
    """plan_steps reflects agent.state.tasks_context.tasks with correct shape."""
    ws, worker_mod = workspace
    _install_fake_agentscope_with_tasks(monkeypatch)

    _FakeAgentWithTasks.tasks = [
        _FakeTask("Research the problem", "completed"),
        _FakeTask("Write solution", "in_progress"),
        _FakeTask("Review output", "pending"),
    ]
    _FakeAgentWithTasks.reply_text = "done planning"
    env.setenv("AGENT_TASK_JSON", json.dumps({"task": "multi-step"}))

    code = worker_mod.main([])

    assert code == 0
    result = json.loads((ws / "result.json").read_text())
    assert result["status"] == "OK"

    steps = result["plan_steps"]
    assert len(steps) == 3

    # n is 1-based
    assert steps[0] == {"n": 1, "t": "Research the problem", "d": True, "live": False}
    assert steps[1] == {"n": 2, "t": "Write solution", "d": False, "live": True}
    assert steps[2] == {"n": 3, "t": "Review output", "d": False, "live": False}


def test_extract_plan_steps_returns_empty_on_missing_state(workspace, env, monkeypatch):
    """_extract_plan_steps never raises; returns [] when agent has no state attr."""
    import agentscope_ext.runtime_worker.__main__ as worker_mod

    class _BrokenAgent:
        pass  # no .state attribute at all

    steps = worker_mod._extract_plan_steps(_BrokenAgent())
    assert steps == []
