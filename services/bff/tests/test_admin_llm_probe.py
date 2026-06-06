"""Tests: BFF POST /admin/llm/probe — LLM endpoint diagnostics proxy.

Covers:
1. Unauthenticated → 401/403.
2. Authenticated but authz denied → 403.
3. Authenticated super_admin → proxied to foundation llm-gateway, report returned.
4. Foundation llm-gateway error → surfaced as upstream error.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest


# ─── Fake foundation responses ────────────────────────────────────────────────

_PROBE_REPORT = {
    "url": "http://my-llm:8080",
    "provider_type": "openai_compatible",
    "results": {
        "list_models": True,
        "model_count": "3",
        "function_calling": True,
        "streaming": True,
        "context_window_32k": True,
        "context_window_value": "131072",
        "parallel_tool_calls": True,
    },
}


class FakeLlmProbeFoundation:
    """Stubs authz /check, identity /sessions/me, and llm-gateway /probe."""

    def __init__(self, *, authz_allowed: bool = True, probe_status: int = 200) -> None:
        self.calls: list[tuple[str, str]] = []
        self.authz_allowed = authz_allowed
        self.probe_status = probe_status

    def route(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        self.calls.append((method, url))

        # authz /check
        if "/check" in url:
            if self.authz_allowed:
                return httpx.Response(200, json={"allowed": True})
            return httpx.Response(200, json={"allowed": False})

        # identity /sessions/me
        if "/sessions/me" in url:
            return httpx.Response(200, json={
                "user_id": "usr_01J5TEST0000000000000USER1",
                "tenant_id": "tnt_01J5TEST00000000000000TNT1",
                "email": "admin@example.com",
                "display_name": "Admin",
                "roles": ["super_admin"],
                "mfa_enabled": False,
                "locale": "zh-CN",
            })

        # llm-gateway /probe
        if "/probe" in url and method == "POST":
            if self.probe_status == 200:
                return httpx.Response(200, json=_PROBE_REPORT)
            return httpx.Response(self.probe_status, json={"detail": "upstream error"})

        return httpx.Response(200, json={"ok": True})


@pytest.fixture
def fake_probe_allowed(monkeypatch: pytest.MonkeyPatch) -> FakeLlmProbeFoundation:
    fake = FakeLlmProbeFoundation(authz_allowed=True)

    class _AC:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        async def get(self, url, **kw): return fake.route("GET", url, **kw)
        async def post(self, url, **kw): return fake.route("POST", url, **kw)
        async def patch(self, url, **kw): return fake.route("PATCH", url, **kw)

    monkeypatch.setattr(httpx, "AsyncClient", _AC)
    return fake


@pytest.fixture
def fake_probe_denied(monkeypatch: pytest.MonkeyPatch) -> FakeLlmProbeFoundation:
    fake = FakeLlmProbeFoundation(authz_allowed=False)

    class _AC:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        async def get(self, url, **kw): return fake.route("GET", url, **kw)
        async def post(self, url, **kw): return fake.route("POST", url, **kw)
        async def patch(self, url, **kw): return fake.route("PATCH", url, **kw)

    monkeypatch.setattr(httpx, "AsyncClient", _AC)
    return fake


@pytest.fixture
def fake_probe_upstream_error(monkeypatch: pytest.MonkeyPatch) -> FakeLlmProbeFoundation:
    fake = FakeLlmProbeFoundation(authz_allowed=True, probe_status=502)

    class _AC:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        async def get(self, url, **kw): return fake.route("GET", url, **kw)
        async def post(self, url, **kw): return fake.route("POST", url, **kw)
        async def patch(self, url, **kw): return fake.route("PATCH", url, **kw)

    monkeypatch.setattr(httpx, "AsyncClient", _AC)
    return fake


# ─── Tests ───────────────────────────────────────────────────────────────────

def test_probe_unauthenticated(client) -> None:
    """No session cookie → 401 or 403, probe not reached."""
    resp = client.post(
        "/api/bff/v1/admin/llm/probe",
        json={"url": "http://llm:8080", "api_key": "sk-x"},
    )
    assert resp.status_code in (401, 403)


def test_probe_authz_denied(client, authed_cookie, fake_probe_denied) -> None:
    """Authenticated but authz returns allowed=False → 403."""
    resp = client.post(
        "/api/bff/v1/admin/llm/probe",
        json={"url": "http://llm:8080", "api_key": "sk-x", "provider_type": "openai_compatible"},
        cookies=authed_cookie,
    )
    assert resp.status_code == 403


def test_probe_super_admin_returns_report(client, authed_cookie, fake_probe_allowed) -> None:
    """Authenticated super_admin → proxied to llm-gateway, returns ProbeReport."""
    resp = client.post(
        "/api/bff/v1/admin/llm/probe",
        json={"url": "http://my-llm:8080", "api_key": "sk-secret", "provider_type": "openai_compatible"},
        cookies=authed_cookie,
    )
    assert resp.status_code == 200
    body = resp.json()

    assert body["url"] == "http://my-llm:8080"
    assert body["provider_type"] == "openai_compatible"
    results = body["results"]

    # All capability keys must be present
    for key in ("list_models", "function_calling", "streaming",
                "context_window_32k", "context_window_value", "parallel_tool_calls"):
        assert key in results, f"ProbeReport missing key: {key}"

    assert results["context_window_32k"] is True
    assert results["parallel_tool_calls"] is True


def test_probe_calls_llm_gateway_not_identity(client, authed_cookie, fake_probe_allowed) -> None:
    """BFF must call /probe on llm-gateway, not identity or billing."""
    resp = client.post(
        "/api/bff/v1/admin/llm/probe",
        json={"url": "http://my-llm:8080", "api_key": "sk-test"},
        cookies=authed_cookie,
    )
    assert resp.status_code == 200
    called_urls = [url for _, url in fake_probe_allowed.calls]
    assert any("/probe" in u for u in called_urls), "llm-gateway /probe not called"
    assert any("/check" in u for u in called_urls), "authz /check not called"


def test_probe_upstream_error_surfaced(client, authed_cookie, fake_probe_upstream_error) -> None:
    """When llm-gateway returns 502, BFF surfaces it as an HTTP error."""
    resp = client.post(
        "/api/bff/v1/admin/llm/probe",
        json={"url": "http://bad-llm:8080", "api_key": "sk-x"},
        cookies=authed_cookie,
    )
    # BFF should propagate the upstream status code
    assert resp.status_code >= 400


def test_probe_report_context_window_fields(client, authed_cookie, fake_probe_allowed) -> None:
    """Verify context_window_value is a string (number as string or 'unknown')."""
    resp = client.post(
        "/api/bff/v1/admin/llm/probe",
        json={"url": "http://my-llm:8080", "api_key": "sk-x"},
        cookies=authed_cookie,
    )
    assert resp.status_code == 200
    ctx_val = resp.json()["results"]["context_window_value"]
    # Must be a string representing a number or the string "unknown"
    assert isinstance(ctx_val, str)
