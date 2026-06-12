"""Tests: BFF admin metrics (Prometheus) proxy.

Covers: unauthenticated 401/403, authz-denied 403, happy 200, downstream
unreachable 502, downstream 4xx pass-through, query-string assembly,
range/step validation.
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
CALLER_TENANT = "tnt_01J5TEST00000000000000TNT1"
PROM_URL = "http://prom-test:9090"


@pytest.fixture
def m_settings() -> Settings:
    return Settings(
        env="test",
        log_level="WARNING",
        cookie_secret=TEST_COOKIE_SECRET,
        cookie_secure=False,
        foundation_identity_url="http://identity-test/api/identity/v1",
        session_orchestrator_url="http://orch-test:8006/api/session/v1",
        prometheus_url=PROM_URL,
        cors_origins="http://localhost",
        otel_enabled=False,
    )


@pytest.fixture
def m_client(m_settings: Settings) -> Iterator[TestClient]:
    app = create_app(m_settings)
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture
def m_cookie(m_settings: Settings) -> dict[str, str]:
    from foundation_sdk import CookieSigner, CookieSignerSettings

    signer = CookieSigner(
        CookieSignerSettings(
            secret_key=m_settings.cookie_secret,
            salt="agent-bff-session",
            max_age_seconds=m_settings.cookie_max_age,
        ),
    )
    token = signer.sign(
        {
            "user_id": "usr_01J5TEST0000000000000USER1",
            "tenant_id": CALLER_TENANT,
            "access_token": "fake-jwt-for-test",
        },
    )
    return {m_settings.cookie_name: token}


class FakeMetrics:
    def __init__(self, *, authz_allowed: bool = True, prom_status: int = 200) -> None:
        self.authz_allowed = authz_allowed
        self.prom_status = prom_status
        self.requests: list[tuple[str, str, dict, dict]] = []

    def route(self, method: str, url: str, **kw: Any) -> httpx.Response:
        self.requests.append(
            (method, url, dict(kw.get("headers") or {}), dict(kw.get("params") or {})),
        )
        if url.endswith("/check"):
            return httpx.Response(200, json={"allowed": self.authz_allowed})
        if "/api/v1/query_range" in url:
            if self.prom_status != 200:
                return httpx.Response(
                    self.prom_status,
                    json={"status": "error", "errorType": "bad_data", "error": "boom"},
                )
            return httpx.Response(
                200,
                json={
                    "status": "success",
                    "data": {"resultType": "matrix", "result": []},
                },
            )
        return httpx.Response(200, json={"ok": True})


def _install(fake: FakeMetrics, monkeypatch: pytest.MonkeyPatch) -> FakeMetrics:
    class _AC:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        async def get(self, url, **kw): return fake.route("GET", url, **kw)
        async def post(self, url, **kw): return fake.route("POST", url, **kw)

    monkeypatch.setattr(httpx, "AsyncClient", _AC)
    return fake


@pytest.fixture
def m_allowed(monkeypatch: pytest.MonkeyPatch) -> FakeMetrics:
    return _install(FakeMetrics(authz_allowed=True), monkeypatch)


@pytest.fixture
def m_denied(monkeypatch: pytest.MonkeyPatch) -> FakeMetrics:
    return _install(FakeMetrics(authz_allowed=False), monkeypatch)


@pytest.fixture
def m_prom_400(monkeypatch: pytest.MonkeyPatch) -> FakeMetrics:
    return _install(FakeMetrics(authz_allowed=True, prom_status=400), monkeypatch)


@pytest.fixture
def m_unreachable(monkeypatch: pytest.MonkeyPatch) -> FakeMetrics:
    fake = FakeMetrics(authz_allowed=True)

    class _AC:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None

        async def get(self, url, **kw):
            if url.endswith("/check"):
                return fake.route("GET", url, **kw)
            raise httpx.ConnectError("prom down")

        async def post(self, url, **kw):
            return fake.route("POST", url, **kw)

    monkeypatch.setattr(httpx, "AsyncClient", _AC)
    return fake


SERIES_PATH = "/admin/metrics/storage-quotas/series"


def test_unauthenticated(m_client: TestClient) -> None:
    resp = m_client.get(PREFIX + SERIES_PATH)
    assert resp.status_code in (401, 403)


def test_authz_denied(m_client, m_cookie, m_denied) -> None:
    resp = m_client.get(PREFIX + SERIES_PATH, cookies=m_cookie)
    assert resp.status_code == 403


def test_happy_path_passthrough(m_client, m_cookie, m_allowed) -> None:
    resp = m_client.get(
        PREFIX + SERIES_PATH + "?range=24h&step=5m&purpose=workspace",
        cookies=m_cookie,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "success"
    assert body["data"]["resultType"] == "matrix"


def test_query_is_built_from_label_filters(m_client, m_cookie, m_allowed) -> None:
    m_client.get(
        PREFIX + SERIES_PATH
        + "?range=1h&step=5m&purpose=workspace&tenant_id=tnt_X",
        cookies=m_cookie,
    )
    prom_calls = [
        params for (_, u, _, params) in m_allowed.requests
        if "/api/v1/query_range" in u
    ]
    assert prom_calls
    q = prom_calls[-1]["query"]
    assert "storage_quota_used_bytes{" in q
    assert 'bucket_purpose="workspace"' in q
    assert 'tenant_id="tnt_X"' in q


def test_query_without_filters_is_bare_metric(m_client, m_cookie, m_allowed) -> None:
    m_client.get(PREFIX + SERIES_PATH, cookies=m_cookie)
    prom_calls = [
        params for (_, u, _, params) in m_allowed.requests
        if "/api/v1/query_range" in u
    ]
    assert prom_calls
    assert prom_calls[-1]["query"] == "storage_quota_used_bytes"


def test_invalid_range_400(m_client, m_cookie, m_allowed) -> None:
    resp = m_client.get(PREFIX + SERIES_PATH + "?range=bogus", cookies=m_cookie)
    assert resp.status_code == 400


def test_range_too_large_400(m_client, m_cookie, m_allowed) -> None:
    resp = m_client.get(PREFIX + SERIES_PATH + "?range=30d", cookies=m_cookie)
    assert resp.status_code == 400


def test_invalid_label_value_400(m_client, m_cookie, m_allowed) -> None:
    """An attempt to inject a quote / brace into a label value is rejected."""
    resp = m_client.get(
        PREFIX + SERIES_PATH + '?purpose=workspace"}+or+1=1+%23',
        cookies=m_cookie,
    )
    assert resp.status_code == 400


def test_prom_400_passthrough(m_client, m_cookie, m_prom_400) -> None:
    resp = m_client.get(PREFIX + SERIES_PATH, cookies=m_cookie)
    assert resp.status_code == 400


def test_prom_unreachable_502(m_client, m_cookie, m_unreachable) -> None:
    resp = m_client.get(PREFIX + SERIES_PATH, cookies=m_cookie)
    assert resp.status_code == 502
