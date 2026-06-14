"""Tests for ``agentscope_ext.runtime_worker.redis_state``.

Uses ``unittest.mock`` to stub ``redis.Redis`` so the tests run without a
live Redis server and without the ``redis`` package being installed in the
test virtualenv (the test marks the import as unavailable when needed via
``sys.modules`` injection, matching the pattern used in
``test_runtime_worker.py``).
"""

from __future__ import annotations

import json
import sys
import types
from unittest.mock import MagicMock, call

import pytest


# ---------------------------------------------------------------------------
# Helpers to install / uninstall a fake redis package
# ---------------------------------------------------------------------------


def _make_fake_redis_module() -> tuple[types.ModuleType, MagicMock]:
    """Return (fake redis module, mock Redis client instance).

    The mock client's ``ping`` and ``get``/``set`` methods are pre-wired so
    tests can assert on them without extra setup.
    """
    mock_client_instance = MagicMock()
    mock_client_instance.ping.return_value = True
    mock_client_instance.get.return_value = None  # default: no existing state

    mock_redis_class = MagicMock(return_value=mock_client_instance)
    mock_redis_class.from_url = MagicMock(return_value=mock_client_instance)

    redis_mod = types.ModuleType("redis")
    redis_mod.Redis = mock_redis_class

    return redis_mod, mock_client_instance


@pytest.fixture
def fake_redis(monkeypatch):
    """Install a fake ``redis`` module; yield (module, mock_client_instance)."""
    mod, instance = _make_fake_redis_module()
    monkeypatch.setitem(sys.modules, "redis", mod)
    # Force reload so the module picks up the patched sys.modules.
    import importlib
    import agentscope_ext.runtime_worker.redis_state as _rs
    importlib.reload(_rs)
    yield mod, instance
    # Reload again to restore clean state for subsequent tests.
    importlib.reload(_rs)


@pytest.fixture
def no_redis(monkeypatch):
    """Make ``import redis`` raise ImportError."""
    monkeypatch.setitem(sys.modules, "redis", None)
    import importlib
    import agentscope_ext.runtime_worker.redis_state as _rs
    importlib.reload(_rs)
    yield
    importlib.reload(_rs)


# ---------------------------------------------------------------------------
# RunStateClient — basic wiring
# ---------------------------------------------------------------------------


def test_client_disabled_when_no_redis_url(no_redis):
    """When REDIS_URL is not provided the client is a silent no-op."""
    from agentscope_ext.runtime_worker.redis_state import RunStateClient

    client = RunStateClient(run_id="run_01", redis_url=None)
    # All operations must not raise.
    client.write_step(plan="p", cursor="c")
    client.mark_completed()
    client.mark_failed()
    assert client.read_checkpoint() is None


def test_client_disabled_when_run_id_empty(fake_redis):
    """When run_id is empty the client is a no-op even if redis_url is set."""
    from agentscope_ext.runtime_worker.redis_state import RunStateClient

    client = RunStateClient(run_id="", redis_url="redis://localhost:6379/0")
    client.write_step(plan="p", cursor="c")
    assert client.read_checkpoint() is None


def test_client_disabled_when_redis_import_fails(no_redis):
    """ImportError from redis package → no-op client, no crash."""
    from agentscope_ext.runtime_worker.redis_state import RunStateClient

    client = RunStateClient(run_id="run_01", redis_url="redis://localhost:6379/0")
    # Should not raise despite redis being unavailable.
    client.write_step()
    assert client._client is None  # noqa: SLF001


def test_client_connects_and_pings(fake_redis):
    """Successful connection calls from_url + ping."""
    redis_mod, mock_instance = fake_redis
    from agentscope_ext.runtime_worker.redis_state import RunStateClient

    client = RunStateClient(run_id="run_abc", redis_url="redis://:pw@host:6379/0")
    redis_mod.Redis.from_url.assert_called_once_with(
        "redis://:pw@host:6379/0",
        socket_connect_timeout=2,
        socket_timeout=2,
        decode_responses=True,
    )
    mock_instance.ping.assert_called_once()
    assert client._client is mock_instance  # noqa: SLF001


def test_ping_failure_disables_client(fake_redis):
    """If ping raises, the client falls back to no-op (failure-open)."""
    _, mock_instance = fake_redis
    mock_instance.ping.side_effect = ConnectionError("refused")

    from agentscope_ext.runtime_worker.redis_state import RunStateClient

    client = RunStateClient(run_id="run_abc", redis_url="redis://host:6379/0")
    assert client._client is None  # noqa: SLF001
    # No raise on subsequent operations.
    client.write_step()


# ---------------------------------------------------------------------------
# write_step
# ---------------------------------------------------------------------------


def test_write_step_increments_step(fake_redis):
    """Each write_step call bumps the step counter."""
    _, mock_instance = fake_redis
    from agentscope_ext.runtime_worker.redis_state import RunStateClient

    client = RunStateClient(run_id="run_01", redis_url="redis://host:6379/0")

    client.write_step(plan="plan-A", cursor="TextBlockStartEvent")
    client.write_step(plan="plan-A", cursor="TextBlockDeltaEvent")

    assert mock_instance.set.call_count == 2
    # First call: step=1
    first_args = mock_instance.set.call_args_list[0]
    first_payload = json.loads(first_args[0][1])
    assert first_payload["step"] == 1
    assert first_payload["plan"] == "plan-A"
    assert first_payload["cursor"] == "TextBlockStartEvent"
    assert first_payload["status"] == "RUNNING"

    # Second call: step=2
    second_payload = json.loads(mock_instance.set.call_args_list[1][0][1])
    assert second_payload["step"] == 2
    assert second_payload["cursor"] == "TextBlockDeltaEvent"


def test_write_step_key_uses_run_id(fake_redis):
    """The Redis key must be ``agent:run:{run_id}:state``."""
    _, mock_instance = fake_redis
    from agentscope_ext.runtime_worker.redis_state import RunStateClient

    client = RunStateClient(run_id="run_XYZ", redis_url="redis://host:6379/0")
    client.write_step()

    key_used = mock_instance.set.call_args[0][0]
    assert key_used == "agent:run:run_XYZ:state"


def test_write_step_failure_open(fake_redis):
    """Redis.set failure must not propagate — only a warning is logged."""
    _, mock_instance = fake_redis
    mock_instance.set.side_effect = Exception("disk full")

    from agentscope_ext.runtime_worker.redis_state import RunStateClient

    client = RunStateClient(run_id="run_01", redis_url="redis://host:6379/0")
    client.write_step()  # must not raise


# ---------------------------------------------------------------------------
# read_checkpoint
# ---------------------------------------------------------------------------


def test_read_checkpoint_returns_none_when_no_key(fake_redis):
    _, mock_instance = fake_redis
    mock_instance.get.return_value = None
    from agentscope_ext.runtime_worker.redis_state import RunStateClient

    client = RunStateClient(run_id="run_01", redis_url="redis://host:6379/0")
    assert client.read_checkpoint() is None


def test_read_checkpoint_returns_parsed_json(fake_redis):
    _, mock_instance = fake_redis
    stored = {"run_id": "run_01", "step": 5, "plan": "my plan", "status": "RUNNING"}
    mock_instance.get.return_value = json.dumps(stored)

    from agentscope_ext.runtime_worker.redis_state import RunStateClient

    client = RunStateClient(run_id="run_01", redis_url="redis://host:6379/0")
    result = client.read_checkpoint()

    assert result == stored


def test_read_checkpoint_failure_open(fake_redis):
    _, mock_instance = fake_redis
    mock_instance.get.side_effect = Exception("network error")

    from agentscope_ext.runtime_worker.redis_state import RunStateClient

    client = RunStateClient(run_id="run_01", redis_url="redis://host:6379/0")
    assert client.read_checkpoint() is None  # no raise


# ---------------------------------------------------------------------------
# mark_completed / mark_failed
# ---------------------------------------------------------------------------


def test_mark_completed_sets_status(fake_redis):
    _, mock_instance = fake_redis
    stored = {"run_id": "run_01", "step": 3, "plan": "", "status": "RUNNING",
              "cursor": "", "last_event_seq": 3, "updated_step": 3}
    mock_instance.get.return_value = json.dumps(stored)

    from agentscope_ext.runtime_worker.redis_state import RunStateClient

    client = RunStateClient(run_id="run_01", redis_url="redis://host:6379/0")
    client.mark_completed()

    written = json.loads(mock_instance.set.call_args[0][1])
    assert written["status"] == "COMPLETED"


def test_mark_failed_sets_status(fake_redis):
    _, mock_instance = fake_redis
    mock_instance.get.return_value = None  # no prior state

    from agentscope_ext.runtime_worker.redis_state import RunStateClient

    client = RunStateClient(run_id="run_01", redis_url="redis://host:6379/0")
    client.mark_failed()

    written = json.loads(mock_instance.set.call_args[0][1])
    assert written["status"] == "FAILED"


# ---------------------------------------------------------------------------
# from_env helper
# ---------------------------------------------------------------------------


def test_from_env_reads_env_vars(fake_redis, monkeypatch):
    """``from_env()`` builds a client from REDIS_URL + RUN_ID env vars."""
    monkeypatch.setenv("REDIS_URL", "redis://:pw@foundation-redis:6379/0")
    monkeypatch.setenv("RUN_ID", "run_env_test")

    from agentscope_ext.runtime_worker import redis_state as rs
    import importlib
    importlib.reload(rs)

    client = rs.from_env()
    assert client._run_id == "run_env_test"  # noqa: SLF001


def test_from_env_noop_when_no_env(no_redis, monkeypatch):
    """``from_env()`` returns a no-op client when env vars are absent."""
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.delenv("RUN_ID", raising=False)

    from agentscope_ext.runtime_worker import redis_state as rs
    import importlib
    importlib.reload(rs)

    client = rs.from_env()
    client.write_step()  # must not raise
    assert client._client is None  # noqa: SLF001


# ---------------------------------------------------------------------------
# push_token / push_token_end — token streaming contract
# ---------------------------------------------------------------------------


def test_push_token_xadd_calls_contract(fake_redis):
    """push_token issues XADD with type=delta, text=<delta>, MAXLEN~10000."""
    _, mock_instance = fake_redis
    from agentscope_ext.runtime_worker.redis_state import RunStateClient, _TOKEN_STREAM_MAXLEN

    client = RunStateClient(run_id="run_tok", redis_url="redis://host:6379/0")
    client.push_token("Hello")
    client.push_token(", world")

    assert mock_instance.xadd.call_count == 2
    # First call
    first_args, first_kwargs = mock_instance.xadd.call_args_list[0]
    assert first_args[0] == "agent:run:run_tok:tokens"
    assert first_args[1] == {"type": "delta", "text": "Hello"}
    assert first_kwargs.get("maxlen") == _TOKEN_STREAM_MAXLEN
    assert first_kwargs.get("approximate") is True
    # Second call
    second_args, _ = mock_instance.xadd.call_args_list[1]
    assert second_args[1] == {"type": "delta", "text": ", world"}


def test_push_token_end_xadd_type_end(fake_redis):
    """push_token_end issues XADD with type=end, text=''."""
    _, mock_instance = fake_redis
    from agentscope_ext.runtime_worker.redis_state import RunStateClient, _TOKEN_STREAM_MAXLEN

    client = RunStateClient(run_id="run_tok_end", redis_url="redis://host:6379/0")
    client.push_token_end()

    assert mock_instance.xadd.call_count == 1
    args, kwargs = mock_instance.xadd.call_args
    assert args[0] == "agent:run:run_tok_end:tokens"
    assert args[1] == {"type": "end", "text": ""}
    assert kwargs.get("maxlen") == _TOKEN_STREAM_MAXLEN
    assert kwargs.get("approximate") is True


def test_push_token_sets_expire_on_first_call(fake_redis):
    """The first push_token call must set EXPIRE on the stream key."""
    _, mock_instance = fake_redis
    from agentscope_ext.runtime_worker.redis_state import RunStateClient, STATE_TTL_SECONDS

    client = RunStateClient(run_id="run_exp", redis_url="redis://host:6379/0")
    # No expire call before first push.
    assert mock_instance.expire.call_count == 0

    client.push_token("hi")
    mock_instance.expire.assert_called_once_with(
        "agent:run:run_exp:tokens", STATE_TTL_SECONDS
    )

    # Second push must NOT call expire again.
    client.push_token("there")
    assert mock_instance.expire.call_count == 1


def test_push_token_end_sets_expire_when_no_deltas(fake_redis):
    """push_token_end alone must also set EXPIRE on the stream key."""
    _, mock_instance = fake_redis
    from agentscope_ext.runtime_worker.redis_state import RunStateClient, STATE_TTL_SECONDS

    client = RunStateClient(run_id="run_exp2", redis_url="redis://host:6379/0")
    client.push_token_end()
    mock_instance.expire.assert_called_once_with(
        "agent:run:run_exp2:tokens", STATE_TTL_SECONDS
    )


def test_push_token_failure_open(fake_redis):
    """XADD failure must not propagate — only a warning is logged."""
    _, mock_instance = fake_redis
    mock_instance.xadd.side_effect = Exception("connection reset")

    from agentscope_ext.runtime_worker.redis_state import RunStateClient

    client = RunStateClient(run_id="run_fail", redis_url="redis://host:6379/0")
    client.push_token("boom")  # must not raise


def test_push_token_end_failure_open(fake_redis):
    """XADD failure in push_token_end must not propagate."""
    _, mock_instance = fake_redis
    mock_instance.xadd.side_effect = Exception("timeout")

    from agentscope_ext.runtime_worker.redis_state import RunStateClient

    client = RunStateClient(run_id="run_fail_end", redis_url="redis://host:6379/0")
    client.push_token_end()  # must not raise


def test_push_token_noop_when_no_client(no_redis):
    """push_token and push_token_end are silent no-ops when Redis is unavailable."""
    from agentscope_ext.runtime_worker.redis_state import RunStateClient

    client = RunStateClient(run_id="run_noop", redis_url=None)
    client.push_token("x")  # must not raise
    client.push_token_end()  # must not raise


# ---------------------------------------------------------------------------
# push_tool_call / push_tool_result / push_thinking — streaming contract
# ---------------------------------------------------------------------------


def test_push_tool_call_xadd_contract(fake_redis):
    """push_tool_call issues XADD with correct type/tool_call_id/name/args fields."""
    _, mock_instance = fake_redis
    from agentscope_ext.runtime_worker.redis_state import RunStateClient, _TOKEN_STREAM_MAXLEN

    client = RunStateClient(run_id="run_tc", redis_url="redis://host:6379/0")
    client.push_tool_call("tc-1", "Read", '{"file": "src/auth.ts", "limit": 142}')

    assert mock_instance.xadd.call_count == 1
    args, kwargs = mock_instance.xadd.call_args
    assert args[0] == "agent:run:run_tc:tokens"
    assert args[1] == {
        "type": "tool_call",
        "tool_call_id": "tc-1",
        "name": "Read",
        "args": '{"file": "src/auth.ts", "limit": 142}',
    }
    assert kwargs.get("maxlen") == _TOKEN_STREAM_MAXLEN
    assert kwargs.get("approximate") is True


def test_push_tool_result_xadd_contract(fake_redis):
    """push_tool_result issues XADD with correct type/tool_call_id/name/result/state."""
    _, mock_instance = fake_redis
    from agentscope_ext.runtime_worker.redis_state import RunStateClient, _TOKEN_STREAM_MAXLEN

    client = RunStateClient(run_id="run_tr", redis_url="redis://host:6379/0")
    client.push_tool_result("tc-2", "Read", "import session from ...", "success")

    assert mock_instance.xadd.call_count == 1
    args, kwargs = mock_instance.xadd.call_args
    assert args[0] == "agent:run:run_tr:tokens"
    fields = args[1]
    assert fields["type"] == "tool_result"
    assert fields["tool_call_id"] == "tc-2"
    assert fields["name"] == "Read"
    assert fields["result"] == "import session from ..."
    assert fields["state"] == "success"
    assert kwargs.get("maxlen") == _TOKEN_STREAM_MAXLEN
    assert kwargs.get("approximate") is True


def test_push_tool_result_truncates_large_output(fake_redis):
    """Results larger than 8 KB must be truncated with '…[truncated]' suffix."""
    _, mock_instance = fake_redis
    from agentscope_ext.runtime_worker.redis_state import RunStateClient

    client = RunStateClient(run_id="run_trunc", redis_url="redis://host:6379/0")
    big_result = "x" * (9 * 1024)  # 9 KB, over the 8 KB limit
    client.push_tool_result("tc-trunc", "Grep", big_result, "success")

    args, _ = mock_instance.xadd.call_args
    stored_result = args[1]["result"]
    assert stored_result.endswith("…[truncated]")
    # The stored result must be at most 8 KB + overhead of the truncation suffix.
    assert len(stored_result.encode("utf-8")) <= 9 * 1024  # definitely under 9 KB


def test_push_tool_result_no_truncation_for_small_output(fake_redis):
    """Results under 8 KB must be stored verbatim."""
    _, mock_instance = fake_redis
    from agentscope_ext.runtime_worker.redis_state import RunStateClient

    client = RunStateClient(run_id="run_notrunc", redis_url="redis://host:6379/0")
    small_result = "line 1\nline 2\nline 3"
    client.push_tool_result("tc-small", "Read", small_result, "success")

    args, _ = mock_instance.xadd.call_args
    assert args[1]["result"] == small_result


def test_push_thinking_xadd_contract(fake_redis):
    """push_thinking issues XADD with type=thinking, text=<thought>."""
    _, mock_instance = fake_redis
    from agentscope_ext.runtime_worker.redis_state import RunStateClient, _TOKEN_STREAM_MAXLEN

    client = RunStateClient(run_id="run_think", redis_url="redis://host:6379/0")
    client.push_thinking("I need to read the file first")

    assert mock_instance.xadd.call_count == 1
    args, kwargs = mock_instance.xadd.call_args
    assert args[0] == "agent:run:run_think:tokens"
    assert args[1] == {"type": "thinking", "text": "I need to read the file first"}
    assert kwargs.get("maxlen") == _TOKEN_STREAM_MAXLEN
    assert kwargs.get("approximate") is True


def test_push_tool_call_failure_open(fake_redis):
    """XADD failure in push_tool_call must not propagate."""
    _, mock_instance = fake_redis
    mock_instance.xadd.side_effect = Exception("connection reset")

    from agentscope_ext.runtime_worker.redis_state import RunStateClient

    client = RunStateClient(run_id="run_tc_fail", redis_url="redis://host:6379/0")
    client.push_tool_call("tc-x", "Read", "{}")  # must not raise


def test_push_tool_result_failure_open(fake_redis):
    """XADD failure in push_tool_result must not propagate."""
    _, mock_instance = fake_redis
    mock_instance.xadd.side_effect = Exception("timeout")

    from agentscope_ext.runtime_worker.redis_state import RunStateClient

    client = RunStateClient(run_id="run_tr_fail", redis_url="redis://host:6379/0")
    client.push_tool_result("tc-x", "Read", "output", "success")  # must not raise


def test_push_tool_call_noop_when_no_client(no_redis):
    """push_tool_call/push_tool_result/push_thinking are no-ops without Redis."""
    from agentscope_ext.runtime_worker.redis_state import RunStateClient

    client = RunStateClient(run_id="run_noop2", redis_url=None)
    client.push_tool_call("tc-1", "Read", "{}")  # must not raise
    client.push_tool_result("tc-1", "Read", "out", "success")  # must not raise
    client.push_thinking("thought")  # must not raise


def test_push_tool_call_sets_expire_on_first_call(fake_redis):
    """The first push_tool_call must set EXPIRE on the stream key."""
    _, mock_instance = fake_redis
    from agentscope_ext.runtime_worker.redis_state import RunStateClient, STATE_TTL_SECONDS

    client = RunStateClient(run_id="run_tc_exp", redis_url="redis://host:6379/0")
    assert mock_instance.expire.call_count == 0

    client.push_tool_call("tc-1", "Read", "{}")
    mock_instance.expire.assert_called_once_with(
        "agent:run:run_tc_exp:tokens", STATE_TTL_SECONDS
    )

    # Second call must NOT call expire again.
    client.push_tool_call("tc-2", "Grep", '{"pattern": "foo"}')
    assert mock_instance.expire.call_count == 1


# ---------------------------------------------------------------------------
# Integration: resume detection in _run_real (worker entrypoint)
# ---------------------------------------------------------------------------


def test_worker_detects_checkpoint_and_injects_memory(monkeypatch, tmp_path):
    """When Redis has a non-terminal checkpoint, _run_real injects a resume note
    into the task memory field so the agent sees 'resuming from step N'."""
    import importlib
    import types

    # Install fake agentscope
    agentscope = types.ModuleType("agentscope")
    agent_mod = types.ModuleType("agentscope.agent")

    class _FakeTextBlock:
        def __init__(self, *, type="text", text=""):
            self.type = type
            self.text = text

    class _FakeMsg:
        def __init__(self, name, content, role):
            self.name = name
            self.content = content
            self.role = role

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

    captured_system_prompt = []

    class _FakeAgent:
        def __init__(self, *, name, system_prompt, model, toolkit, **_kw):
            self.name = name
            self.system_prompt = system_prompt
            captured_system_prompt.append(system_prompt)
        async def reply_stream(self, msg):
            yield TextBlockStartEvent("blk-1")
            yield TextBlockDeltaEvent("blk-1", "done")
            yield ReplyEndEvent()

    agent_mod.Agent = _FakeAgent
    message_mod = types.ModuleType("agentscope.message")
    message_mod.Msg = _FakeMsg
    message_mod.TextBlock = _FakeTextBlock
    formatter_mod = types.ModuleType("agentscope.formatter")
    formatter_mod.AnthropicChatFormatter = lambda: "fmt"
    model_mod = types.ModuleType("agentscope.model")
    credential_mod = types.ModuleType("agentscope.credential")

    class _FC:
        def __init__(self, **kw): pass

    class _FAnthropic:
        def __init__(self, *, credential, model, stream=False, formatter=None):
            self.model_name = model
            self.kind = "anthropic"

    credential_mod.AnthropicCredential = _FC
    model_mod.AnthropicChatModel = _FAnthropic
    tool_mod = types.ModuleType("agentscope.tool")

    class _FT:
        def __init__(self, tools=None, **_kw): self.tools = list(tools or [])

    tool_mod.Toolkit = _FT
    tool_mod.TaskCreate = type("TaskCreate", (), {})
    tool_mod.TaskUpdate = type("TaskUpdate", (), {})
    tool_mod.TaskList = type("TaskList", (), {})

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

    # Install fake redis that returns a RUNNING checkpoint at step 7.
    redis_mod, mock_instance = _make_fake_redis_module()
    checkpoint = {"run_id": "run_chk", "step": 7, "plan": "analyse data", "status": "RUNNING",
                  "cursor": "ThinkEvent", "last_event_seq": 7, "updated_step": 7}
    mock_instance.get.return_value = json.dumps(checkpoint)
    monkeypatch.setitem(sys.modules, "redis", redis_mod)

    # Set env vars so RunStateClient activates.
    monkeypatch.setenv("REDIS_URL", "redis://:pw@host:6379/0")
    monkeypatch.setenv("RUN_ID", "run_chk")
    monkeypatch.setenv("FOUNDATION_API_URL", "https://foundation.local")
    monkeypatch.setenv("FOUNDATION_API_TOKEN", "tok")
    monkeypatch.setenv("FOUNDATION_TENANT_ID", "tnt-abc")
    monkeypatch.setenv("FOUNDATION_USER_ID", "usr-xyz")
    monkeypatch.setenv("LLM_MODEL", "claude-3-5-sonnet-latest")
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("LLM_API_KEY", "sk-fake")

    import agentscope_ext.runtime_worker.__main__ as worker
    import agentscope_ext.runtime_worker.redis_state as rs
    importlib.reload(rs)
    importlib.reload(worker)
    monkeypatch.setattr(worker, "WORKSPACE", tmp_path)

    task = {"task": "continue the analysis"}
    status, summary, _usage, _plan_steps = worker._run_real(task)

    assert status == "OK"
    # The resume note must be injected into the system prompt via the memory path.
    assert len(captured_system_prompt) == 1
    prompt = captured_system_prompt[0]
    assert "断点恢复" in prompt or "step 7" in prompt or "7" in prompt


# ---------------------------------------------------------------------------
# Broker egress mode — RunStateClient routes through the tool-broker façade
# ---------------------------------------------------------------------------


class _FakeHTTPResponse:
    def __init__(self, *, json_body=None, status_code=200) -> None:
        self._json = json_body or {}
        self.status_code = status_code

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeHTTPX:
    """Records broker HTTP calls; returns canned responses keyed by path."""

    def __init__(self) -> None:
        self.posts: list[dict] = []
        self.gets: list[dict] = []
        self.get_value = None

    def post(self, url, *, json=None, headers=None, timeout=None):
        self.posts.append({"url": url, "json": json, "headers": headers})
        return _FakeHTTPResponse(json_body={"ok": True})

    def get(self, url, *, params=None, headers=None, timeout=None):
        self.gets.append({"url": url, "params": params, "headers": headers})
        return _FakeHTTPResponse(json_body={"value": self.get_value})


@pytest.fixture
def broker_env(monkeypatch):
    """Configure broker egress mode env vars (REDIS_URL blank, broker set)."""
    monkeypatch.setenv("RUN_ID", "run_brk")
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.setenv("TOOL_BROKER_URL", "http://tool-broker:8024/api/tool-broker/v1")
    monkeypatch.setenv("TOOL_BROKER_LEASE", "v1.body.sig")
    monkeypatch.setenv("FOUNDATION_TENANT_ID", "tnt_brk")
    monkeypatch.setenv("FOUNDATION_USER_ID", "usr_brk")
    monkeypatch.setenv("FOUNDATION_API_TOKEN", "tok_brk")


def _install_fake_httpx(monkeypatch) -> _FakeHTTPX:
    fake = _FakeHTTPX()
    httpx_mod = types.ModuleType("httpx")
    httpx_mod.get = fake.get
    httpx_mod.post = fake.post
    monkeypatch.setitem(sys.modules, "httpx", httpx_mod)
    return fake


def test_broker_mode_built_when_redis_url_blank_and_broker_set(broker_env, monkeypatch):
    """from_env builds a broker transport when REDIS_URL is blank + broker set."""
    _install_fake_httpx(monkeypatch)
    import importlib
    from agentscope_ext.runtime_worker import redis_state as rs
    importlib.reload(rs)

    client = rs.from_env()
    assert client._client is not None  # noqa: SLF001
    assert type(client._client).__name__ == "_BrokerTransport"  # noqa: SLF001


def test_broker_mode_disabled_without_lease(monkeypatch):
    """No lease → no broker transport, silent no-op (not a crash)."""
    monkeypatch.setenv("RUN_ID", "run_brk")
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.setenv("TOOL_BROKER_URL", "http://tool-broker:8024/api/tool-broker/v1")
    monkeypatch.delenv("TOOL_BROKER_LEASE", raising=False)
    import importlib
    from agentscope_ext.runtime_worker import redis_state as rs
    importlib.reload(rs)

    client = rs.from_env()
    assert client._client is None  # noqa: SLF001
    client.write_step()  # must not raise


def test_broker_write_step_posts_to_state_set(broker_env, monkeypatch):
    fake = _install_fake_httpx(monkeypatch)
    import importlib
    from agentscope_ext.runtime_worker import redis_state as rs
    importlib.reload(rs)

    client = rs.from_env()
    client.write_step(plan="p", cursor="c")

    assert len(fake.posts) == 1
    post = fake.posts[0]
    assert post["url"].endswith("/state/v1/set")
    assert post["json"]["run_id"] == "run_brk"
    assert post["json"]["kind"] == "state"
    assert post["json"]["ttl"] == rs.STATE_TTL_SECONDS
    body = json.loads(post["json"]["value"])
    assert body["plan"] == "p"
    assert body["cursor"] == "c"
    assert body["status"] == "RUNNING"
    # Lease + tenant headers are forwarded.
    assert post["headers"]["X-Tool-Broker-Lease"] == "v1.body.sig"
    assert post["headers"]["X-Tenant-Id"] == "tnt_brk"
    assert post["headers"]["Authorization"] == "Bearer tok_brk"


def test_broker_push_token_posts_to_xadd_with_expire(broker_env, monkeypatch):
    fake = _install_fake_httpx(monkeypatch)
    import importlib
    from agentscope_ext.runtime_worker import redis_state as rs
    importlib.reload(rs)

    client = rs.from_env()
    client.push_token("hello")
    client.push_token_end()

    xadds = [p for p in fake.posts if p["url"].endswith("/state/v1/xadd")]
    assert len(xadds) == 2
    assert xadds[0]["json"]["fields"] == {"type": "delta", "text": "hello"}
    assert xadds[0]["json"]["maxlen"] == rs._TOKEN_STREAM_MAXLEN
    assert xadds[0]["json"]["expire"] == rs.STATE_TTL_SECONDS
    assert xadds[1]["json"]["fields"] == {"type": "end", "text": ""}


def test_broker_read_checkpoint_gets_from_state(broker_env, monkeypatch):
    fake = _install_fake_httpx(monkeypatch)
    fake.get_value = json.dumps({"run_id": "run_brk", "step": 4, "status": "RUNNING"})
    import importlib
    from agentscope_ext.runtime_worker import redis_state as rs
    importlib.reload(rs)

    client = rs.from_env()
    checkpoint = client.read_checkpoint()

    assert checkpoint == {"run_id": "run_brk", "step": 4, "status": "RUNNING"}
    assert len(fake.gets) == 1
    assert fake.gets[0]["url"].endswith("/state/v1/get")
    assert fake.gets[0]["params"] == {"run_id": "run_brk", "kind": "state"}


def test_broker_write_paused_posts_paused_kind(broker_env, monkeypatch):
    fake = _install_fake_httpx(monkeypatch)
    import importlib
    from agentscope_ext.runtime_worker import redis_state as rs
    importlib.reload(rs)

    client = rs.from_env()
    client.write_paused({"kind": "confirm", "reply_id": "r1"})

    paused = [p for p in fake.posts if p["json"].get("kind") == "paused"]
    assert len(paused) == 1
    assert paused[0]["json"]["ttl"] == rs.PAUSED_TTL_SECONDS
    assert paused[0]["url"].endswith("/state/v1/set")


def test_broker_transport_failure_open(broker_env, monkeypatch):
    """A broker HTTP error must be swallowed (failure-open), not raised."""
    import importlib

    def _boom_post(*_a, **_k):
        raise RuntimeError("connection refused")

    def _boom_get(*_a, **_k):
        raise RuntimeError("connection refused")

    httpx_mod = types.ModuleType("httpx")
    httpx_mod.post = _boom_post
    httpx_mod.get = _boom_get
    monkeypatch.setitem(sys.modules, "httpx", httpx_mod)

    from agentscope_ext.runtime_worker import redis_state as rs
    importlib.reload(rs)

    client = rs.from_env()
    client.write_step()  # must not raise
    client.push_token("x")  # must not raise
    assert client.read_checkpoint() is None  # must not raise
