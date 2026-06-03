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
        def __init__(self): self.tools = []

    tool_mod.Toolkit = _FT

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
    status, summary = worker._run_real(task)

    assert status == "OK"
    # The resume note must be injected into the system prompt via the memory path.
    assert len(captured_system_prompt) == 1
    prompt = captured_system_prompt[0]
    assert "断点恢复" in prompt or "step 7" in prompt or "7" in prompt
