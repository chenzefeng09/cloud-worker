"""mTLS plumbing for CubeSandboxRuntime — ADR 0001 §5 follow-up.

These tests pin the contract on :meth:`CubeSandboxRuntime._make_client`:

* ``mtls`` (the default and production setting) loads cert+key+CA into
  the :class:`httpx.Client`; a missing piece is a deployment error, not
  a silent downgrade to plain TLS.
* ``tls`` verifies the CA only.
* ``off`` returns a plain HTTP client — for local CubeProxy stubs.

We intercept the ``httpx.Client`` constructor rather than going through
the network so the assertions stay on the request-time configuration.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from agent_scheduler.config import Settings
from agent_scheduler.services.cube_runtime import CubeSandboxRuntime


def _settings(**over: Any) -> Settings:
    defaults: dict[str, Any] = {
        "env": "test",
        "log_level": "WARNING",
        "otel_enabled": False,
        "runtime": "cube",
        "cube_controller_url": "https://cube-test:8443",
    }
    defaults.update(over)
    return Settings(**defaults)


def _capture_client_kwargs() -> tuple[list[dict[str, Any]], Any]:
    """Patch ``httpx.Client`` and capture each constructor's kwargs."""
    captured: list[dict[str, Any]] = []

    class _StubClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            captured.append(kwargs)

    return captured, _StubClient


# --------------------------------------------------------------------------- #
# mode == "mtls" (default)
# --------------------------------------------------------------------------- #


def test_mtls_mode_loads_cert_key_and_ca(tmp_path) -> None:
    cert = tmp_path / "client.crt"
    key = tmp_path / "client.key"
    ca = tmp_path / "ca.crt"
    for p in (cert, key, ca):
        p.write_text("--stub--")
    settings = _settings(
        cube_tls_mode="mtls",
        cube_client_cert=str(cert),
        cube_client_key=str(key),
        cube_ca_cert=str(ca),
    )

    captured, stub = _capture_client_kwargs()
    with patch("agent_scheduler.services.cube_runtime.httpx.Client", stub):
        CubeSandboxRuntime(settings)

    assert len(captured) == 1
    kwargs = captured[0]
    assert kwargs["verify"] == str(ca)
    assert kwargs["cert"] == (str(cert), str(key))
    assert kwargs["base_url"] == "https://cube-test:8443"


def test_mtls_mode_default_when_unset() -> None:
    """``mtls`` is the default — confirm Settings agrees."""
    settings = _settings()
    assert settings.cube_tls_mode == "mtls"


def test_mtls_mode_missing_client_cert_raises_runtime_error(tmp_path) -> None:
    ca = tmp_path / "ca.crt"
    ca.write_text("--stub--")
    settings = _settings(
        cube_tls_mode="mtls",
        cube_client_cert=None,
        cube_client_key=None,
        cube_ca_cert=str(ca),
    )
    with pytest.raises(RuntimeError, match="requires cube_client_cert"):
        CubeSandboxRuntime(settings)


def test_mtls_mode_missing_ca_raises_runtime_error() -> None:
    settings = _settings(
        cube_tls_mode="mtls",
        cube_client_cert="/x/c",
        cube_client_key="/x/k",
        cube_ca_cert=None,
    )
    with pytest.raises(RuntimeError, match="requires cube_ca_cert"):
        CubeSandboxRuntime(settings)


# --------------------------------------------------------------------------- #
# mode == "tls"
# --------------------------------------------------------------------------- #


def test_tls_mode_only_verifies_ca(tmp_path) -> None:
    ca = tmp_path / "ca.crt"
    ca.write_text("--stub--")
    settings = _settings(cube_tls_mode="tls", cube_ca_cert=str(ca))

    captured, stub = _capture_client_kwargs()
    with patch("agent_scheduler.services.cube_runtime.httpx.Client", stub):
        CubeSandboxRuntime(settings)

    kwargs = captured[0]
    assert kwargs["verify"] == str(ca)
    assert "cert" not in kwargs, "tls mode must not present a client cert"


def test_tls_mode_missing_ca_raises_runtime_error() -> None:
    settings = _settings(cube_tls_mode="tls", cube_ca_cert=None)
    with pytest.raises(RuntimeError, match="requires cube_ca_cert"):
        CubeSandboxRuntime(settings)


# --------------------------------------------------------------------------- #
# mode == "off"
# --------------------------------------------------------------------------- #


def test_off_mode_builds_plain_http_client() -> None:
    """No cert material loaded even when set — dev-only knob."""
    settings = _settings(
        cube_tls_mode="off",
        cube_client_cert="/ignored",
        cube_client_key="/ignored",
        cube_ca_cert="/ignored",
    )

    captured, stub = _capture_client_kwargs()
    with patch("agent_scheduler.services.cube_runtime.httpx.Client", stub):
        CubeSandboxRuntime(settings)

    kwargs = captured[0]
    assert "verify" not in kwargs
    assert "cert" not in kwargs
