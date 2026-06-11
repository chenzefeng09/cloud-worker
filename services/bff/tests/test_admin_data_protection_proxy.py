"""Tests: BFF data-protection admin proxy.

Covers for every endpoint: unauthenticated 401/403, authz-denied 403,
super_admin passthrough 2xx, downstream 4xx 透传, downstream unreachable 502,
cross-tenant ?tenant_id= header override, X-User-Id forwarding.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from agent_bff.config import Settings
from agent_bff.main import create_app

PREFIX = "/api/bff/v1"
TEST_COOKIE_SECRET = "abcdefghijklmnopqrstuvwxyz0123456789ABCDEFGHIJKL"

DP_URL = "http://dp-test/api/data-protection/v1"
CALLER_TENANT = "tnt_01J5TEST00000000000000TNT1"


@pytest.fixture
def dp_settings() -> Settings:
    return Settings(
        env="test",
        log_level="WARNING",
        cookie_secret=TEST_COOKIE_SECRET,
        cookie_secure=False,
        foundation_identity_url="http://identity-test/api/identity/v1",
        session_orchestrator_url="http://orch-test:8006/api/session/v1",
        foundation_data_protection_url=DP_URL,
        cors_origins="http://localhost",
        otel_enabled=False,
    )


@pytest.fixture
def dp_client(dp_settings: Settings) -> Iterator[TestClient]:
    app = create_app(dp_settings)
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture
def dp_cookie(dp_settings: Settings) -> dict[str, str]:
    from foundation_sdk import CookieSigner, CookieSignerSettings

    signer = CookieSigner(
        CookieSignerSettings(
            secret_key=dp_settings.cookie_secret,
            salt="agent-bff-session",
            max_age_seconds=dp_settings.cookie_max_age,
        ),
    )
    token = signer.sign(
        {
            "user_id": "usr_01J5TEST0000000000000USER1",
            "tenant_id": CALLER_TENANT,
            "access_token": "fake-jwt-for-test",
        },
    )
    return {dp_settings.cookie_name: token}


class FakeDP:
    def __init__(self, *, authz_allowed: bool = True, error_status: int | None = None) -> None:
        self.authz_allowed = authz_allowed
        self.error_status = error_status
        self.requests: list[tuple[str, str, dict, dict]] = []

    def route(self, method: str, url: str, **kw: Any) -> httpx.Response:
        self.requests.append(
            (method, url, dict(kw.get("headers") or {}), dict(kw.get("params") or {})),
        )

        if url.endswith("/authz/v1/check"):
            return httpx.Response(200, json={"allowed": self.authz_allowed})

        if self.error_status is not None:
            return httpx.Response(
                self.error_status,
                json={"title": "upstream error", "status": self.error_status},
            )

        if "/masking-rules" in url:
            return httpx.Response(200, json={"items": []})
        if "/data-residency" in url:
            return httpx.Response(200, json={"region": "eu"})
        if "/cross-border/check" in url:
            return httpx.Response(200, json={"allowed": True})
        if "/destruction" in url:
            if "/approve" in url:
                return httpx.Response(200, json={"id": "dr_1", "status": "APPROVED"})
            if method == "POST":
                return httpx.Response(201, json={"id": "dr_new"})
            return httpx.Response(200, json={"id": "dr_1", "status": "PENDING"})
        if "/consent" in url:
            if method == "POST":
                return httpx.Response(201, json={"id": "con_new"})
            return httpx.Response(200, json={"items": []})

        return httpx.Response(200, json={"ok": True})


def _install(fake: FakeDP, monkeypatch: pytest.MonkeyPatch) -> FakeDP:
    class _AC:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        async def get(self, url, **kw): return fake.route("GET", url, **kw)
        async def post(self, url, **kw): return fake.route("POST", url, **kw)
        async def put(self, url, **kw): return fake.route("PUT", url, **kw)
        async def patch(self, url, **kw): return fake.route("PATCH", url, **kw)
        async def delete(self, url, **kw): return fake.route("DELETE", url, **kw)

    monkeypatch.setattr(httpx, "AsyncClient", _AC)
    return fake


@pytest.fixture
def dp_allowed(monkeypatch: pytest.MonkeyPatch) -> FakeDP:
    return _install(FakeDP(authz_allowed=True), monkeypatch)


@pytest.fixture
def dp_denied(monkeypatch: pytest.MonkeyPatch) -> FakeDP:
    return _install(FakeDP(authz_allowed=False), monkeypatch)


@pytest.fixture
def dp_404(monkeypatch: pytest.MonkeyPatch) -> FakeDP:
    return _install(FakeDP(authz_allowed=True, error_status=404), monkeypatch)


@pytest.fixture
def dp_unreachable(monkeypatch: pytest.MonkeyPatch) -> FakeDP:
    fake = FakeDP(authz_allowed=True)

    class _AC:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None

        async def _maybe(self, method, url, **kw):
            if url.endswith("/authz/v1/check"):
                return fake.route(method, url, **kw)
            raise httpx.ConnectError("downstream down")

        async def get(self, url, **kw): return await self._maybe("GET", url, **kw)
        async def post(self, url, **kw): return await self._maybe("POST", url, **kw)
        async def put(self, url, **kw): return await self._maybe("PUT", url, **kw)
        async def patch(self, url, **kw): return await self._maybe("PATCH", url, **kw)
        async def delete(self, url, **kw): return await self._maybe("DELETE", url, **kw)

    monkeypatch.setattr(httpx, "AsyncClient", _AC)
    return fake


# (method, path, json-body, happy-status)
ENDPOINTS = [
    ("GET",    "/admin/data-protection/masking-rules", None, 200),
    ("GET",    "/admin/data-protection/data-residency", None, 200),
    ("POST",   "/admin/data-protection/cross-border/check", {"destination_country": "US", "data_categories": ["pii"]}, 200),
    ("POST",   "/admin/data-protection/destruction", {"resource_ids": ["r1"], "method": "crypto-shred"}, 201),
    ("GET",    "/admin/data-protection/destruction/dr_1", None, 200),
    ("POST",   "/admin/data-protection/destruction/dr_1/approve", {}, 200),
    ("GET",    "/admin/data-protection/consent", None, 200),
    ("POST",   "/admin/data-protection/consent", {"user_id": "u", "consent_type": "marketing", "purpose": "p", "granted": True, "ip_address": "1.2.3.4"}, 201),
]


def _call(client: TestClient, method: str, path: str, body, **kw):
    fn = getattr(client, method.lower())
    if body is not None and method in ("POST", "PUT", "PATCH"):
        return fn(PREFIX + path, json=body, **kw)
    return fn(PREFIX + path, **kw)


@pytest.mark.parametrize("method,path,body,_ok", ENDPOINTS)
def test_unauthenticated_rejected(dp_client, method, path, body, _ok) -> None:
    resp = _call(dp_client, method, path, body)
    assert resp.status_code in (401, 403), f"{method} {path} → {resp.status_code}"


@pytest.mark.parametrize("method,path,body,_ok", ENDPOINTS)
def test_authz_denied_403(dp_client, dp_cookie, dp_denied, method, path, body, _ok) -> None:
    resp = _call(dp_client, method, path, body, cookies=dp_cookie)
    assert resp.status_code == 403, f"{method} {path} → {resp.status_code}"


@pytest.mark.parametrize("method,path,body,ok", ENDPOINTS)
def test_happy_path_passthrough(dp_client, dp_cookie, dp_allowed, method, path, body, ok) -> None:
    resp = _call(dp_client, method, path, body, cookies=dp_cookie)
    assert resp.status_code == ok, f"{method} {path} → {resp.status_code} ({resp.text})"


@pytest.mark.parametrize("method,path,body,_ok", ENDPOINTS)
def test_downstream_4xx_passthrough(dp_client, dp_cookie, dp_404, method, path, body, _ok) -> None:
    resp = _call(dp_client, method, path, body, cookies=dp_cookie)
    assert resp.status_code == 404, f"{method} {path} → {resp.status_code}"


@pytest.mark.parametrize("method,path,body,_ok", ENDPOINTS)
def test_downstream_unreachable_502(dp_client, dp_cookie, dp_unreachable, method, path, body, _ok) -> None:
    resp = _call(dp_client, method, path, body, cookies=dp_cookie)
    assert resp.status_code == 502, f"{method} {path} → {resp.status_code}"


def test_hits_data_protection_url(dp_client, dp_cookie, dp_allowed) -> None:
    dp_client.get(PREFIX + "/admin/data-protection/masking-rules", cookies=dp_cookie)
    assert any(
        u == f"{DP_URL}/masking-rules" for (_, u, _, _) in dp_allowed.requests
    )


def test_consent_user_id_query_forwarded(dp_client, dp_cookie, dp_allowed) -> None:
    dp_client.get(
        PREFIX + "/admin/data-protection/consent?user_id=usr_xyz", cookies=dp_cookie,
    )
    calls = [p for (_, u, _, p) in dp_allowed.requests if u == f"{DP_URL}/consent"]
    assert calls
    assert calls[-1].get("user_id") == "usr_xyz"


def test_approve_forwards_user_id_header(dp_client, dp_cookie, dp_allowed) -> None:
    dp_client.post(
        PREFIX + "/admin/data-protection/destruction/dr_1/approve", json={}, cookies=dp_cookie,
    )
    calls = [
        h for (_, u, h, _) in dp_allowed.requests if "/destruction/dr_1/approve" in u
    ]
    assert calls and calls[-1].get("X-User-Id") == "usr_01J5TEST0000000000000USER1"


def test_tenant_id_override_changes_header(dp_client, dp_cookie, dp_allowed) -> None:
    target = "tnt_OTHER0000000000000000000001"
    dp_client.get(
        PREFIX + f"/admin/data-protection/masking-rules?tenant_id={target}", cookies=dp_cookie,
    )
    calls = [
        (h, p) for (_, u, h, p) in dp_allowed.requests
        if u == f"{DP_URL}/masking-rules"
    ]
    assert calls
    headers, params = calls[-1]
    assert headers.get("X-Tenant-Id") == target
    assert "tenant_id" not in params


def test_tenant_id_absent_uses_caller_tenant(dp_client, dp_cookie, dp_allowed) -> None:
    dp_client.get(PREFIX + "/admin/data-protection/masking-rules", cookies=dp_cookie)
    calls = [
        h for (_, u, h, _) in dp_allowed.requests if u == f"{DP_URL}/masking-rules"
    ]
    assert calls and calls[-1].get("X-Tenant-Id") == CALLER_TENANT
