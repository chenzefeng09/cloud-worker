"""Phase 5: feature-flag-driven worker image routing.

Two concerns are exercised here:

1. ``evaluate_flag`` itself — that it is failure-open (any error → False) and
   reads the ``enabled`` field on the happy path.  We stub ``httpx.Client`` so
   the request/response shapes are pinned and no network is touched.

2. The runtime ``spawn_run`` paths (docker + cube) select the right image and
   command/cmd based on the flag, and fall back to the AgentScope image when
   the flag service is unreachable or times out.  We patch ``evaluate_flag``
   where each runtime module imports it.

Regression guard: the AgentScope path (flag disabled / flag service down) must
keep the exact image + python argv it had before Phase 5.
"""

from __future__ import annotations

import tempfile
from unittest.mock import MagicMock

import httpx
import pytest

from agent_scheduler.config import Settings
from agent_scheduler.services.cube_runtime import CubeSandboxRuntime
from agent_scheduler.services.docker_runtime import DockerRuntime
from agent_scheduler.services.feature_flag import (
    evaluate_flag,
    runtime_extensions_for_run,
)

_TENANT = "tnt_01J5TNT0000000000000000001"
_USER = "usr_01J5USR0000000000000000001"

_CLAUDE_IMAGE = "ghcr.io/agent-platform/claude-sdk-sandbox:0.1"
_AGENT_IMAGE = "hello-world:latest"  # matches the `settings` fixture


# --------------------------------------------------------------------------- #
# evaluate_flag — happy path + failure-open
# --------------------------------------------------------------------------- #


class _FakeClient:
    """Stand-in for ``httpx.Client`` used as a context manager."""

    def __init__(self, *, response: httpx.Response | None = None,
                 raises: Exception | None = None, **_: object) -> None:
        self._response = response
        self._raises = raises
        self.last_post: dict[str, object] = {}

    def __enter__(self) -> _FakeClient:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def post(self, url: str, **kw: object) -> httpx.Response:
        self.last_post = {"url": url, **kw}
        if self._raises is not None:
            raise self._raises
        assert self._response is not None
        return self._response


def _resp(data: dict, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=data, request=httpx.Request("POST", "/x"))


def test_evaluate_flag_returns_enabled_true(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(response=_resp({"enabled": True, "variant": "on", "reason": "rule"}))
    monkeypatch.setattr(
        "agent_scheduler.services.feature_flag.httpx.Client",
        lambda **kw: fake,
    )
    out = evaluate_flag(
        "enable_claude_sdk_worker",
        tenant_id=_TENANT, user_id=_USER,
        flag_url="http://feature-flag:8012/api/feature-flag/v1",
    )
    assert out is True
    # Contract: POST to /evaluate with flag + context, tenant/user headers.
    assert fake.last_post["url"].endswith("/evaluate")
    assert fake.last_post["json"] == {
        "flag": "enable_claude_sdk_worker",
        "context": {"tenant_id": _TENANT, "user_id": _USER},
    }
    assert fake.last_post["headers"] == {"X-Tenant-Id": _TENANT, "X-User-Id": _USER}


def test_evaluate_flag_returns_enabled_false(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(response=_resp({"enabled": False, "variant": None, "reason": "off"}))
    monkeypatch.setattr(
        "agent_scheduler.services.feature_flag.httpx.Client", lambda **kw: fake,
    )
    assert evaluate_flag(
        "enable_claude_sdk_worker", tenant_id=_TENANT, user_id=_USER,
        flag_url="http://feature-flag:8012/api/feature-flag/v1",
    ) is False


def test_evaluate_flag_failure_open_on_connect_error(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(raises=httpx.ConnectError("unreachable"))
    monkeypatch.setattr(
        "agent_scheduler.services.feature_flag.httpx.Client", lambda **kw: fake,
    )
    assert evaluate_flag(
        "enable_claude_sdk_worker", tenant_id=_TENANT, user_id=_USER,
        flag_url="http://feature-flag:8012/api/feature-flag/v1",
    ) is False


def test_evaluate_flag_failure_open_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(raises=httpx.TimeoutException("slow"))
    monkeypatch.setattr(
        "agent_scheduler.services.feature_flag.httpx.Client", lambda **kw: fake,
    )
    assert evaluate_flag(
        "enable_claude_sdk_worker", tenant_id=_TENANT, user_id=_USER,
        flag_url="http://feature-flag:8012/api/feature-flag/v1",
    ) is False


def test_evaluate_flag_failure_open_on_non_200(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(response=_resp({"detail": "boom"}, status=500))
    monkeypatch.setattr(
        "agent_scheduler.services.feature_flag.httpx.Client", lambda **kw: fake,
    )
    assert evaluate_flag(
        "enable_claude_sdk_worker", tenant_id=_TENANT, user_id=_USER,
        flag_url="http://feature-flag:8012/api/feature-flag/v1",
    ) is False


def test_evaluate_flag_failure_open_on_malformed_body(monkeypatch: pytest.MonkeyPatch) -> None:
    # 200 but no usable JSON → .json() raises → failure-open.
    bad = httpx.Response(200, content=b"not json", request=httpx.Request("POST", "/x"))
    fake = _FakeClient(response=bad)
    monkeypatch.setattr(
        "agent_scheduler.services.feature_flag.httpx.Client", lambda **kw: fake,
    )
    assert evaluate_flag(
        "enable_claude_sdk_worker", tenant_id=_TENANT, user_id=_USER,
        flag_url="http://feature-flag:8012/api/feature-flag/v1",
    ) is False


# --------------------------------------------------------------------------- #
# runtime_extensions_for_run — canvas_tool flag gating (canvas v2, Phase B)
# --------------------------------------------------------------------------- #


_BASE_EXT = "mcp_tenant_injector,skills_loader,web_fetch"


def test_runtime_extensions_appends_canvas_tool_when_flag_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeClient(response=_resp({"enabled": True}))
    monkeypatch.setattr(
        "agent_scheduler.services.feature_flag.httpx.Client", lambda **kw: fake,
    )
    out = runtime_extensions_for_run(
        base_extensions=_BASE_EXT,
        tenant_id=_TENANT, user_id=_USER,
        flag_url="http://feature-flag:8012/api/feature-flag/v1",
    )
    assert out == _BASE_EXT + ",canvas_tool"
    # Contract: evaluated the ``enable_canvas_tool`` flag.
    assert fake.last_post["json"]["flag"] == "enable_canvas_tool"


def test_runtime_extensions_unchanged_when_flag_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeClient(response=_resp({"enabled": False}))
    monkeypatch.setattr(
        "agent_scheduler.services.feature_flag.httpx.Client", lambda **kw: fake,
    )
    out = runtime_extensions_for_run(
        base_extensions=_BASE_EXT,
        tenant_id=_TENANT, user_id=_USER,
        flag_url="http://feature-flag:8012/api/feature-flag/v1",
    )
    assert out == _BASE_EXT  # zero change


def test_runtime_extensions_unchanged_when_flag_service_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failure-open: an unreachable flag service leaves the base unchanged."""
    fake = _FakeClient(raises=httpx.ConnectError("unreachable"))
    monkeypatch.setattr(
        "agent_scheduler.services.feature_flag.httpx.Client", lambda **kw: fake,
    )
    out = runtime_extensions_for_run(
        base_extensions=_BASE_EXT,
        tenant_id=_TENANT, user_id=_USER,
        flag_url="http://feature-flag:8012/api/feature-flag/v1",
    )
    assert out == _BASE_EXT


def test_runtime_extensions_idempotent_when_already_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A base that already lists canvas_tool is returned unchanged, no dupe,
    and the flag is not even evaluated (no HTTP)."""
    def _boom(**_kw: object):  # pragma: no cover - must not be called
        raise AssertionError("flag should not be evaluated when already present")

    monkeypatch.setattr(
        "agent_scheduler.services.feature_flag.httpx.Client", _boom,
    )
    base = _BASE_EXT + ",canvas_tool"
    assert (
        runtime_extensions_for_run(
            base_extensions=base,
            tenant_id=_TENANT, user_id=_USER,
            flag_url="http://feature-flag:8012/api/feature-flag/v1",
        )
        == base
    )


# --------------------------------------------------------------------------- #
# Docker runtime image routing
# --------------------------------------------------------------------------- #


def _docker_spawn(runtime: DockerRuntime) -> dict:
    """Run a spawn and return the kwargs passed to ``containers.run``."""
    runtime.spawn_run(
        run_id="run_01J5RUN0000000000000IMG001",
        session_id="ses_01J5SES0000000000000IMG001",
        task="hello",
        tenant_id=_TENANT,
        user_id=_USER,
        foundation_token="tok_img",
        llm_virtual_key="vk-test",
    )
    _, kwargs = runtime._client.containers.run.call_args  # noqa: SLF001
    return kwargs


def test_docker_flag_enabled_uses_claude_image(
    runtime: DockerRuntime, mock_docker_client: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "agent_scheduler.services.docker_runtime.evaluate_flag",
        lambda *a, **kw: True,
    )
    mock_docker_client.containers.run.return_value = MagicMock(id="c-claude")
    kwargs = _docker_spawn(runtime)
    assert kwargs["image"] == _CLAUDE_IMAGE
    # command=None → docker uses the image ENTRYPOINT (node).
    assert kwargs["command"] is None


def test_docker_canvas_flag_on_appends_canvas_tool_to_env(
    runtime: DockerRuntime, mock_docker_client: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """enable_canvas_tool on → container env RUNTIME_EXTENSIONS gains canvas_tool."""
    monkeypatch.setattr(
        "agent_scheduler.services.docker_runtime.evaluate_flag",
        lambda *a, **kw: False,  # keep the AgentScope image; isolate the ext var
    )
    # Force the canvas-flag branch on deterministically (no flag-service HTTP).
    monkeypatch.setattr(
        "agent_scheduler.services.docker_runtime.runtime_extensions_for_run",
        lambda *, base_extensions, **kw: base_extensions + ",canvas_tool",
    )
    mock_docker_client.containers.run.return_value = MagicMock(id="c-canvas-on")
    kwargs = _docker_spawn(runtime)
    assert "canvas_tool" in kwargs["environment"]["RUNTIME_EXTENSIONS"].split(",")


def test_docker_canvas_flag_off_env_has_no_canvas_tool(
    runtime: DockerRuntime, mock_docker_client: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """enable_canvas_tool off → RUNTIME_EXTENSIONS is the unmodified base."""
    monkeypatch.setattr(
        "agent_scheduler.services.docker_runtime.evaluate_flag",
        lambda *a, **kw: False,
    )
    monkeypatch.setattr(
        "agent_scheduler.services.docker_runtime.runtime_extensions_for_run",
        lambda *, base_extensions, **kw: base_extensions,  # flag off ⇒ unchanged
    )
    mock_docker_client.containers.run.return_value = MagicMock(id="c-canvas-off")
    kwargs = _docker_spawn(runtime)
    assert "canvas_tool" not in kwargs["environment"]["RUNTIME_EXTENSIONS"].split(",")


def test_docker_flag_disabled_uses_agent_image_regression(
    runtime: DockerRuntime, mock_docker_client: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "agent_scheduler.services.docker_runtime.evaluate_flag",
        lambda *a, **kw: False,
    )
    mock_docker_client.containers.run.return_value = MagicMock(id="c-agent")
    kwargs = _docker_spawn(runtime)
    assert kwargs["image"] == _AGENT_IMAGE
    assert kwargs["command"] == ["python", "-m", "agentscope_ext.runtime_worker"]


def test_docker_flag_service_unreachable_falls_back(
    runtime: DockerRuntime, mock_docker_client: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A connection error inside evaluate_flag returns False → agent image.

    We patch httpx.Client (not evaluate_flag) so the real failure-open code
    path is exercised end-to-end through spawn_run.
    """
    fake = _FakeClient(raises=httpx.ConnectError("unreachable"))
    monkeypatch.setattr(
        "agent_scheduler.services.feature_flag.httpx.Client", lambda **kw: fake,
    )
    mock_docker_client.containers.run.return_value = MagicMock(id="c-fallback")
    kwargs = _docker_spawn(runtime)
    assert kwargs["image"] == _AGENT_IMAGE
    assert kwargs["command"] == ["python", "-m", "agentscope_ext.runtime_worker"]


def test_docker_flag_service_timeout_falls_back(
    runtime: DockerRuntime, mock_docker_client: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeClient(raises=httpx.TimeoutException("slow"))
    monkeypatch.setattr(
        "agent_scheduler.services.feature_flag.httpx.Client", lambda **kw: fake,
    )
    mock_docker_client.containers.run.return_value = MagicMock(id="c-timeout")
    kwargs = _docker_spawn(runtime)
    assert kwargs["image"] == _AGENT_IMAGE
    assert kwargs["command"] == ["python", "-m", "agentscope_ext.runtime_worker"]


def test_docker_flag_false_global_default_true_uses_claude_image(
    runtime: DockerRuntime, mock_docker_client: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Flag False + ``use_claude_sdk_worker_default=True`` → claude image.

    The per-run flag returns False, but the global config fallback flips the
    selection so every run lands on the Claude SDK worker.
    """
    monkeypatch.setattr(
        "agent_scheduler.services.docker_runtime.evaluate_flag",
        lambda *a, **kw: False,
    )
    runtime._settings.use_claude_sdk_worker_default = True  # noqa: SLF001
    mock_docker_client.containers.run.return_value = MagicMock(id="c-default")
    kwargs = _docker_spawn(runtime)
    assert kwargs["image"] == _CLAUDE_IMAGE
    assert kwargs["command"] is None


def test_docker_flag_true_global_default_false_still_claude_image(
    runtime: DockerRuntime, mock_docker_client: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Flag True wins regardless of the (False) global default → claude image."""
    monkeypatch.setattr(
        "agent_scheduler.services.docker_runtime.evaluate_flag",
        lambda *a, **kw: True,
    )
    runtime._settings.use_claude_sdk_worker_default = False  # noqa: SLF001
    mock_docker_client.containers.run.return_value = MagicMock(id="c-flagwins")
    kwargs = _docker_spawn(runtime)
    assert kwargs["image"] == _CLAUDE_IMAGE
    assert kwargs["command"] is None


# --------------------------------------------------------------------------- #
# Cube runtime image routing
# --------------------------------------------------------------------------- #


def _make_cube_runtime(*, use_claude_sdk_worker_default: bool = False) -> CubeSandboxRuntime:
    settings = Settings(
        env="test",
        log_level="WARNING",
        otel_enabled=False,
        runtime="cube",
        cube_controller_url="http://cube-test:8443",
        agent_image=_AGENT_IMAGE,
        # Global config fallback for the Claude SDK worker selection (default
        # False keeps the existing regression behaviour green).
        use_claude_sdk_worker_default=use_claude_sdk_worker_default,
        # Broker mode (the default) now fail-closes without a CubeVS allow_out
        # CIDR list; these image-routing tests don't exercise egress, so give
        # the runtime a valid allowlist to reach the spawn POST.
        cube_sandbox_allow_out="10.96.0.10/32, 10.96.0.53/32",
        # The cube spawn path now writes a 0600 ``.secrets.json`` under
        # ``workspace_base`` (secret-leak hardening); the module default
        # (/var/agent-platform/workspaces) isn't writable in CI, so point it at
        # a tmp dir for these image-routing spawns.
        workspace_base=tempfile.mkdtemp(prefix="cube-imgtest-"),
    )
    client = MagicMock()
    client.post.return_value = httpx.Response(
        201, json={"sandbox_id": "sb_img"}, request=httpx.Request("POST", "/x"),
    )
    rt = CubeSandboxRuntime.__new__(CubeSandboxRuntime)
    rt._settings = settings  # noqa: SLF001
    rt._client = client  # noqa: SLF001
    rt._skill_cache = None  # noqa: SLF001
    return rt


def _cube_spawn(rt: CubeSandboxRuntime) -> dict:
    rt.spawn_run(
        run_id="run_01J5RUN0000000000000IMG002",
        session_id="ses_01J5SES0000000000000IMG002",
        task="hello",
        tenant_id=_TENANT,
        user_id=_USER,
        foundation_token="ft",
        llm_virtual_key="vk-test",
    )
    _, kwargs = rt._client.post.call_args  # noqa: SLF001
    return kwargs["json"]


def test_cube_flag_enabled_uses_claude_image_with_node_cmd(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "agent_scheduler.services.cube_runtime.evaluate_flag",
        lambda *a, **kw: True,
    )
    rt = _make_cube_runtime()
    body = _cube_spawn(rt)
    assert body["template_id"] == _CLAUDE_IMAGE
    # CubeSandbox: explicit node ENTRYPOINT, not an empty cmd.
    assert body["cmd"] == ["node", "/app/dist/main.js"]


def test_cube_flag_disabled_uses_agent_image_regression(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "agent_scheduler.services.cube_runtime.evaluate_flag",
        lambda *a, **kw: False,
    )
    rt = _make_cube_runtime()
    body = _cube_spawn(rt)
    assert body["template_id"] == _AGENT_IMAGE
    assert body["cmd"] == ["python", "-m", "agentscope_ext.runtime_worker"]


def test_cube_flag_service_unreachable_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeClient(raises=httpx.ConnectError("unreachable"))
    monkeypatch.setattr(
        "agent_scheduler.services.feature_flag.httpx.Client", lambda **kw: fake,
    )
    rt = _make_cube_runtime()
    body = _cube_spawn(rt)
    assert body["template_id"] == _AGENT_IMAGE
    assert body["cmd"] == ["python", "-m", "agentscope_ext.runtime_worker"]


def test_cube_flag_false_global_default_true_uses_claude_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Flag False + ``use_claude_sdk_worker_default=True`` → claude image."""
    monkeypatch.setattr(
        "agent_scheduler.services.cube_runtime.evaluate_flag",
        lambda *a, **kw: False,
    )
    rt = _make_cube_runtime(use_claude_sdk_worker_default=True)
    body = _cube_spawn(rt)
    assert body["template_id"] == _CLAUDE_IMAGE
    assert body["cmd"] == ["node", "/app/dist/main.js"]


def test_cube_flag_true_global_default_false_still_claude_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Flag True wins regardless of the (False) global default → claude image."""
    monkeypatch.setattr(
        "agent_scheduler.services.cube_runtime.evaluate_flag",
        lambda *a, **kw: True,
    )
    rt = _make_cube_runtime(use_claude_sdk_worker_default=False)
    body = _cube_spawn(rt)
    assert body["template_id"] == _CLAUDE_IMAGE
    assert body["cmd"] == ["node", "/app/dist/main.js"]
