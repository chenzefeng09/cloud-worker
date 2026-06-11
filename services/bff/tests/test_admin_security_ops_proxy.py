"""Tests: BFF security-ops admin proxy.

Covers for every endpoint:
  1. Unauthenticated → 401 / 403.
  2. Authenticated but authz denied → 403.
  3. Authenticated super_admin → upstream proxied, 2xx body returned.
  4. Upstream 4xx → status 透传 (via _raise_for_upstream).
  5. Upstream unreachable (ConnectError) → 502.
  6. Cross-tenant ?tenant_id= overrides the forwarded X-Tenant-Id header.

Plus security-ops specifics:
  - dotted ``filter.severity`` alias forwarded verbatim.
  - GET /sbom/watch returns the bare downstream array unchanged.
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

SECOPS_URL = "http://secops-test/api/security-ops/v1"
CALLER_TENANT = "tnt_01J5TEST00000000000000TNT1"


@pytest.fixture
def secops_settings() -> Settings:
    return Settings(
        env="test",
        log_level="WARNING",
        cookie_secret=TEST_COOKIE_SECRET,
        cookie_secure=False,
        foundation_identity_url="http://identity-test/api/identity/v1",
        session_orchestrator_url="http://orch-test:8006/api/session/v1",
        foundation_security_ops_url=SECOPS_URL,
        cors_origins="http://localhost",
        otel_enabled=False,
    )


@pytest.fixture
def secops_client(secops_settings: Settings) -> Iterator[TestClient]:
    app = create_app(secops_settings)
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture
def secops_cookie(secops_settings: Settings) -> dict[str, str]:
    from foundation_sdk import CookieSigner, CookieSignerSettings

    signer = CookieSigner(
        CookieSignerSettings(
            secret_key=secops_settings.cookie_secret,
            salt="agent-bff-session",
            max_age_seconds=secops_settings.cookie_max_age,
        ),
    )
    token = signer.sign(
        {
            "user_id": "usr_01J5TEST0000000000000USER1",
            "tenant_id": CALLER_TENANT,
            "access_token": "fake-jwt-for-test",
        },
    )
    return {secops_settings.cookie_name: token}


class FakeSecOps:
    def __init__(self, *, authz_allowed: bool = True, error_status: int | None = None) -> None:
        self.authz_allowed = authz_allowed
        self.error_status = error_status
        self.requests: list[tuple[str, str, dict, dict]] = []

    def route(self, method: str, url: str, **kw: Any) -> httpx.Response:
        self.requests.append(
            (method, url, dict(kw.get("headers") or {}), dict(kw.get("params") or {})),
        )

        if url.endswith("/check"):
            return httpx.Response(200, json={"allowed": self.authz_allowed})

        if self.error_status is not None:
            return httpx.Response(
                self.error_status,
                json={"title": "upstream error", "status": self.error_status},
            )

        if "/sbom/watch" in url:
            if method == "POST":
                return httpx.Response(201, json={"id": "sbom_new"})
            return httpx.Response(200, json=[{"id": "sbom_1"}, {"id": "sbom_2"}])
        if "/vulnerabilities/sla-dashboard" in url:
            return httpx.Response(200, json={"slo": 0.9})
        if "/risk-accept" in url:
            return httpx.Response(202, json={"id": "vul_1", "status": "RISK_ACCEPTED"})
        if "/vulnerabilities" in url:
            if method == "PATCH":
                return httpx.Response(200, json={"id": "vul_1", "status": "FIXED"})
            return httpx.Response(200, json={"items": [{"id": "vul_1"}]})
        if "/incidents" in url:
            if "/resolve" in url:
                return httpx.Response(200, json={"id": "inc_1", "status": "RESOLVED"})
            if method == "PATCH":
                return httpx.Response(200, json={"id": "inc_1"})
            if method == "POST":
                return httpx.Response(201, json={"id": "inc_new"})
            return httpx.Response(200, json={"items": [{"id": "inc_1"}]})
        if "/scans/runs" in url:
            if method == "POST":
                return httpx.Response(201, json={"id": "run_new"})
            return httpx.Response(200, json={"items": []})
        if "/drills" in url:
            if method == "POST":
                return httpx.Response(201, json={"id": "drill_new"})
            return httpx.Response(200, json={"items": []})

        return httpx.Response(200, json={"ok": True})


def _install(fake: FakeSecOps, monkeypatch: pytest.MonkeyPatch) -> FakeSecOps:
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
def secops_allowed(monkeypatch: pytest.MonkeyPatch) -> FakeSecOps:
    return _install(FakeSecOps(authz_allowed=True), monkeypatch)


@pytest.fixture
def secops_denied(monkeypatch: pytest.MonkeyPatch) -> FakeSecOps:
    return _install(FakeSecOps(authz_allowed=False), monkeypatch)


@pytest.fixture
def secops_404(monkeypatch: pytest.MonkeyPatch) -> FakeSecOps:
    return _install(FakeSecOps(authz_allowed=True, error_status=404), monkeypatch)


@pytest.fixture
def secops_unreachable(monkeypatch: pytest.MonkeyPatch) -> FakeSecOps:
    fake = FakeSecOps(authz_allowed=True)

    class _AC:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None

        async def _maybe(self, method, url, **kw):
            if url.endswith("/check"):
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
    ("GET",    "/admin/security-ops/vulnerabilities", None, 200),
    ("GET",    "/admin/security-ops/vulnerabilities/sla-dashboard", None, 200),
    ("GET",    "/admin/security-ops/vulnerabilities/vul_1", None, 200),
    ("PATCH",  "/admin/security-ops/vulnerabilities/vul_1", {"status": "FIXED"}, 200),
    ("POST",   "/admin/security-ops/vulnerabilities/vul_1/risk-accept", {"rationale": "r"}, 202),
    ("GET",    "/admin/security-ops/incidents", None, 200),
    ("POST",   "/admin/security-ops/incidents", {"severity": "high"}, 201),
    ("GET",    "/admin/security-ops/incidents/inc_1", None, 200),
    ("PATCH",  "/admin/security-ops/incidents/inc_1", {"status": "X"}, 200),
    ("POST",   "/admin/security-ops/incidents/inc_1/resolve", {}, 200),
    ("GET",    "/admin/security-ops/scans/runs", None, 200),
    ("POST",   "/admin/security-ops/scans/runs", {"type": "sca"}, 201),
    ("GET",    "/admin/security-ops/sbom/watch", None, 200),
    ("POST",   "/admin/security-ops/sbom/watch", {"package": "p"}, 201),
    ("GET",    "/admin/security-ops/drills", None, 200),
    ("POST",   "/admin/security-ops/drills", {"kind": "tabletop"}, 201),
]


def _call(client: TestClient, method: str, path: str, body, **kw):
    fn = getattr(client, method.lower())
    if body is not None and method in ("POST", "PUT", "PATCH"):
        return fn(PREFIX + path, json=body, **kw)
    return fn(PREFIX + path, **kw)


@pytest.mark.parametrize("method,path,body,_ok", ENDPOINTS)
def test_unauthenticated_rejected(secops_client, method, path, body, _ok) -> None:
    resp = _call(secops_client, method, path, body)
    assert resp.status_code in (401, 403), f"{method} {path} → {resp.status_code}"


@pytest.mark.parametrize("method,path,body,_ok", ENDPOINTS)
def test_authz_denied_403(secops_client, secops_cookie, secops_denied, method, path, body, _ok) -> None:
    resp = _call(secops_client, method, path, body, cookies=secops_cookie)
    assert resp.status_code == 403, f"{method} {path} → {resp.status_code}"


@pytest.mark.parametrize("method,path,body,ok", ENDPOINTS)
def test_happy_path_passthrough(secops_client, secops_cookie, secops_allowed, method, path, body, ok) -> None:
    resp = _call(secops_client, method, path, body, cookies=secops_cookie)
    assert resp.status_code == ok, f"{method} {path} → {resp.status_code} ({resp.text})"


@pytest.mark.parametrize("method,path,body,_ok", ENDPOINTS)
def test_downstream_4xx_passthrough(secops_client, secops_cookie, secops_404, method, path, body, _ok) -> None:
    resp = _call(secops_client, method, path, body, cookies=secops_cookie)
    assert resp.status_code == 404, f"{method} {path} → {resp.status_code}"


@pytest.mark.parametrize("method,path,body,_ok", ENDPOINTS)
def test_downstream_unreachable_502(secops_client, secops_cookie, secops_unreachable, method, path, body, _ok) -> None:
    resp = _call(secops_client, method, path, body, cookies=secops_cookie)
    assert resp.status_code == 502, f"{method} {path} → {resp.status_code}"


def test_hits_security_ops_url(secops_client, secops_cookie, secops_allowed) -> None:
    secops_client.get(PREFIX + "/admin/security-ops/vulnerabilities", cookies=secops_cookie)
    assert any(
        u == f"{SECOPS_URL}/vulnerabilities" for (_, u, _, _) in secops_allowed.requests
    )


def test_dotted_filter_alias_forwarded(secops_client, secops_cookie, secops_allowed) -> None:
    secops_client.get(
        PREFIX + "/admin/security-ops/vulnerabilities?filter.severity=critical&filter.status=open",
        cookies=secops_cookie,
    )
    calls = [
        p for (_, u, _, p) in secops_allowed.requests
        if u == f"{SECOPS_URL}/vulnerabilities"
    ]
    assert calls
    params = calls[-1]
    assert params.get("filter.severity") == "critical"
    assert params.get("filter.status") == "open"


def test_sbom_watch_returns_bare_array(secops_client, secops_cookie, secops_allowed) -> None:
    resp = secops_client.get(PREFIX + "/admin/security-ops/sbom/watch", cookies=secops_cookie)
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)
    assert body == [{"id": "sbom_1"}, {"id": "sbom_2"}]


def test_tenant_id_override_changes_header(secops_client, secops_cookie, secops_allowed) -> None:
    target = "tnt_OTHER0000000000000000000001"
    secops_client.get(
        PREFIX + f"/admin/security-ops/vulnerabilities?tenant_id={target}",
        cookies=secops_cookie,
    )
    calls = [
        (h, p) for (_, u, h, p) in secops_allowed.requests
        if u == f"{SECOPS_URL}/vulnerabilities"
    ]
    assert calls
    headers, params = calls[-1]
    assert headers.get("X-Tenant-Id") == target
    assert "tenant_id" not in params


def test_tenant_id_absent_uses_caller_tenant(secops_client, secops_cookie, secops_allowed) -> None:
    secops_client.get(PREFIX + "/admin/security-ops/vulnerabilities", cookies=secops_cookie)
    calls = [
        h for (_, u, h, _) in secops_allowed.requests
        if u == f"{SECOPS_URL}/vulnerabilities"
    ]
    assert calls and calls[-1].get("X-Tenant-Id") == CALLER_TENANT
