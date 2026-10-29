"""Unit tests for ``PoolCubeRuntime`` — warm-VM spawn request shape.

The pool pre-warms microVMs running the Claude Agent SDK worker image with
its node entry; a stub httpx client captures the POST body so the image and
cmd are pinned without a CubeSandbox controller.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest

from sandbox_pool.config import Settings
from sandbox_pool.cube_runtime import WORKER_CMD, PoolCubeRuntime

_CLAUDE_SDK_IMAGE = "ghcr.io/agent-platform/claude-sdk-sandbox:0.1"


def _runtime(settings: Settings, client: MagicMock) -> PoolCubeRuntime:
    rt = PoolCubeRuntime.__new__(PoolCubeRuntime)
    rt._settings = settings  # noqa: SLF001 — test setup
    rt._client = client  # noqa: SLF001
    return rt


def test_sandbox_image_defaults_to_claude_sdk_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SANDBOX_POOL_SANDBOX_IMAGE", raising=False)
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.sandbox_image == _CLAUDE_SDK_IMAGE


def test_start_idle_container_boots_claude_sdk_worker() -> None:
    client = MagicMock()
    client.post.return_value = httpx.Response(
        201, json={"sandbox_id": "sb_warm"}, request=httpx.Request("POST", "/x"),
    )
    settings = Settings(
        env="test",
        log_level="WARNING",
        otel_enabled=False,
        sandbox_image=_CLAUDE_SDK_IMAGE,
    )
    rt = _runtime(settings, client)

    sid = rt.start_idle_container("base", memory_limit="4g")

    assert sid == "sb_warm"
    _, kwargs = client.post.call_args
    body = kwargs["json"]
    assert body["template_id"] == _CLAUDE_SDK_IMAGE
    assert body["cmd"] == ["node", "/app/dist/main.js"]
    assert body["cmd"] == WORKER_CMD
    assert body["resources"]["memory_mb"] == 4096
    assert body["metadata"]["agent-platform.pool-managed"] == "true"
