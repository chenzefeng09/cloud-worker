"""Tests: BFF governance admin proxies (notification / api-platform / pam /
dlp / compliance).

Covers, for every endpoint of the five new routers:
  1. Unauthenticated → 401 / 403.
  2. Authenticated but authz denied → 403.
  3. Authenticated super_admin → upstream proxied, 2xx body returned.
  4. Upstream 4xx → status + body 透传 (via _raise_for_upstream).
  5. Upstream unreachable (ConnectError) → 502.
  6. Cross-tenant ?tenant_id= overrides the forwarded X-Tenant-Id header and is
     not leaked as a downstream query param.

The routers are wired into ``create_app`` already, but this module builds its
own app with fixed downstream URLs so tests can assert which upstream was hit.
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

NOTIF_URL = "http://notif-test/api/notification/v1"
APIPLAT_URL = "http://apiplat-test/api/api-platform/v1"
PAM_URL = "http://pam-test/api/pam/v1"
DLP_URL = "http://dlp-test/api/dlp/v1"
COMPL_URL = "http://compl-test/api/compliance/v1"

CALLER_TENANT = "tnt_01J5TEST00000000000000TNT1"


# ──────────────────────────────────────────────────────────────────────────────
# App / fixtures
# ──────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def gov_settings() -> Settings:
    return Settings(
        env="test",
        log_level="WARNING",
        cookie_secret=TEST_COOKIE_SECRET,
        cookie_secure=False,
        foundation_identity_url="http://identity-test/api/identity/v1",
        session_orchestrator_url="http://orch-test:8006/api/session/v1",
        foundation_notification_url=NOTIF_URL,
        foundation_api_platform_url=APIPLAT_URL,
        foundation_pam_url=PAM_URL,
        foundation_dlp_url=DLP_URL,
        foundation_compliance_url=COMPL_URL,
        cors_origins="http://localhost",
        otel_enabled=False,
    )


@pytest.fixture
def gov_client(gov_settings: Settings) -> Iterator[TestClient]:
    app = create_app(gov_settings)
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture
def gov_cookie(gov_settings: Settings) -> dict[str, str]:
    from foundation_sdk import CookieSigner, CookieSignerSettings

    signer = CookieSigner(
        CookieSignerSettings(
            secret_key=gov_settings.cookie_secret,
            salt="agent-bff-session",
            max_age_seconds=gov_settings.cookie_max_age,
        ),
    )
    token = signer.sign(
        {
            "user_id": "usr_01J5TEST0000000000000USER1",
            "tenant_id": CALLER_TENANT,
            "access_token": "fake-jwt-for-test",
        },
    )
    return {gov_settings.cookie_name: token}


class FakeGov:
    """Deterministic stub for authz + all five governance downstreams.

    Every forwarded call is recorded in ``requests`` as (method, url, headers,
    params) so cross-tenant header/param assertions are possible.
    """

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

        # ── notification ──────────────────────────────────────────────────────
        if "/templates" in url:
            return httpx.Response(200, json={"items": [{"id": "tpl_1"}]})
        if url.endswith("/send"):
            return httpx.Response(200, json={"id": "ntf_1", "status": "QUEUED"})

        # ── api-platform ──────────────────────────────────────────────────────
        if "/keys" in url:
            if "/revoke" in url:
                return httpx.Response(200, json={"id": "key_1", "status": "REVOKED"})
            if method == "POST":
                return httpx.Response(201, json={"id": "key_new", "secret": "sk_x"})
            return httpx.Response(200, json={"items": [{"id": "key_1"}]})
        if "/oauth-apps" in url:
            if method == "POST":
                return httpx.Response(201, json={"id": "app_new"})
            return httpx.Response(200, json={"items": [{"id": "app_1"}]})
        if "/webhooks" in url:
            if method == "DELETE":
                return httpx.Response(204)
            if method == "POST":
                return httpx.Response(201, json={"id": "wh_new"})
            return httpx.Response(200, json={"items": [{"id": "wh_1"}]})

        # ── pam ────────────────────────────────────────────────────────────────
        if "/elevations/pending" in url:
            return httpx.Response(200, json={"items": []})
        if "/elevations/history" in url:
            return httpx.Response(200, json={"items": []})
        if "/elevations" in url:
            if "/approve" in url:
                return httpx.Response(200, json={"id": "elv_1", "status": "APPROVED"})
            if "/revoke" in url:
                return httpx.Response(200, json={"id": "elv_1", "status": "REVOKED"})
            if method == "POST":
                return httpx.Response(201, json={"id": "elv_new", "status": "PENDING"})
        if "/sessions" in url:
            return httpx.Response(200, json={"items": []})

        # ── dlp ────────────────────────────────────────────────────────────────
        if "/scan" in url:
            return httpx.Response(200, json={"findings": []})
        if "/classification:tag" in url:
            return httpx.Response(201, json={"resource_id": "r1", "level": "PII"})
        if "/classification:get" in url:
            return httpx.Response(200, json={"resource_id": "r1", "level": "PII"})
        if "/check:read" in url:
            return httpx.Response(200, json={"allowed": True})
        if "/check:download" in url:
            return httpx.Response(200, json={"allowed": False})

        # ── compliance ───────────────────────────────────────────────────────
        if "/ropa" in url:
            if method == "POST":
                return httpx.Response(201, json={"id": "ropa_new"})
            return httpx.Response(200, json={"items": [{"id": "ropa_1"}]})
        if "/dpia" in url:
            if "/complete" in url:
                return httpx.Response(200, json={"id": "dpia_1", "status": "COMPLETED"})
            return httpx.Response(201, json={"id": "dpia_new"})
        if "/dsar" in url:
            if method == "POST":
                return httpx.Response(201, json={"id": "dsar_new"})
            return httpx.Response(200, json={"id": "dsar_1", "status": "OPEN"})
        if "/reports" in url:
            if method == "POST":
                return httpx.Response(201, json={"id": "rep_new"})
            return httpx.Response(200, json={"items": [{"id": "rep_1"}]})

        return httpx.Response(200, json={"ok": True})


def _install(fake: FakeGov, monkeypatch: pytest.MonkeyPatch) -> FakeGov:
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
def gov_allowed(monkeypatch: pytest.MonkeyPatch) -> FakeGov:
    return _install(FakeGov(authz_allowed=True), monkeypatch)


@pytest.fixture
def gov_denied(monkeypatch: pytest.MonkeyPatch) -> FakeGov:
    return _install(FakeGov(authz_allowed=False), monkeypatch)


@pytest.fixture
def gov_404(monkeypatch: pytest.MonkeyPatch) -> FakeGov:
    return _install(FakeGov(authz_allowed=True, error_status=404), monkeypatch)


@pytest.fixture
def gov_unreachable(monkeypatch: pytest.MonkeyPatch) -> FakeGov:
    fake = FakeGov(authz_allowed=True)

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


# ──────────────────────────────────────────────────────────────────────────────
# Endpoint matrix: (method, path, json-body, happy-status)
# ──────────────────────────────────────────────────────────────────────────────

ENDPOINTS = [
    # notification
    ("GET",    "/admin/notification/templates", None, 200),
    ("POST",   "/admin/notification/send", {"template_id": "t", "to": "u"}, 200),
    # api-platform
    ("GET",    "/admin/api-platform/keys", None, 200),
    ("POST",   "/admin/api-platform/keys", {"name": "k"}, 201),
    ("POST",   "/admin/api-platform/keys/key_1/revoke", {}, 200),
    ("GET",    "/admin/api-platform/oauth-apps", None, 200),
    ("POST",   "/admin/api-platform/oauth-apps", {"name": "a"}, 201),
    ("GET",    "/admin/api-platform/webhooks", None, 200),
    ("POST",   "/admin/api-platform/webhooks", {"url": "https://x"}, 201),
    ("DELETE", "/admin/api-platform/webhooks/wh_1", None, 200),
    # pam
    ("GET",    "/admin/pam/elevations/pending", None, 200),
    ("GET",    "/admin/pam/elevations/history", None, 200),
    ("GET",    "/admin/pam/sessions", None, 200),
    ("POST",   "/admin/pam/elevations", {"reason": "r"}, 201),
    ("POST",   "/admin/pam/elevations/elv_1/approve", {}, 200),
    ("POST",   "/admin/pam/elevations/elv_1/revoke", {}, 200),
    # dlp
    ("POST",   "/admin/dlp/scan", {"content": "c"}, 200),
    ("POST",   "/admin/dlp/classification:tag", {"resource_type": "x", "resource_id": "r"}, 201),
    ("GET",    "/admin/dlp/classification:get?resource_type=x&resource_id=r", None, 200),
    ("POST",   "/admin/dlp/check:read", {"resource_type": "x", "resource_id": "r"}, 200),
    ("POST",   "/admin/dlp/check:download", {"resource_type": "x", "resource_id": "r"}, 200),
    # compliance
    ("GET",    "/admin/compliance/ropa", None, 200),
    ("POST",   "/admin/compliance/ropa", {"name": "n"}, 201),
    ("POST",   "/admin/compliance/dpia", {"name": "n"}, 201),
    ("POST",   "/admin/compliance/dpia/dpia_1/complete", {}, 200),
    (
        "POST",
        "/admin/compliance/dsar",
        {
            "user_id": "usr_01J5TEST0000000000000USER1",
            "request_type": "access",
            "details": "Copy of all personal data held about me",
        },
        201,
    ),
    ("GET",    "/admin/compliance/dsar/dsar_1", None, 200),
    ("GET",    "/admin/compliance/reports", None, 200),
    ("POST",   "/admin/compliance/reports", {"kind": "k"}, 201),
]


def _call(client: TestClient, method: str, path: str, body, **kw):
    fn = getattr(client, method.lower())
    if body is not None and method in ("POST", "PUT", "PATCH"):
        return fn(PREFIX + path, json=body, **kw)
    return fn(PREFIX + path, **kw)


@pytest.mark.parametrize("method,path,body,_ok", ENDPOINTS)
def test_unauthenticated_rejected(gov_client, method, path, body, _ok) -> None:
    resp = _call(gov_client, method, path, body)
    assert resp.status_code in (401, 403), f"{method} {path} → {resp.status_code}"


@pytest.mark.parametrize("method,path,body,_ok", ENDPOINTS)
def test_authz_denied_403(gov_client, gov_cookie, gov_denied, method, path, body, _ok) -> None:
    resp = _call(gov_client, method, path, body, cookies=gov_cookie)
    assert resp.status_code == 403, f"{method} {path} → {resp.status_code}"


@pytest.mark.parametrize("method,path,body,ok", ENDPOINTS)
def test_happy_path_passthrough(gov_client, gov_cookie, gov_allowed, method, path, body, ok) -> None:
    resp = _call(gov_client, method, path, body, cookies=gov_cookie)
    assert resp.status_code == ok, f"{method} {path} → {resp.status_code} ({resp.text})"


@pytest.mark.parametrize("method,path,body,_ok", ENDPOINTS)
def test_downstream_4xx_passthrough(gov_client, gov_cookie, gov_404, method, path, body, _ok) -> None:
    resp = _call(gov_client, method, path, body, cookies=gov_cookie)
    assert resp.status_code == 404, f"{method} {path} → {resp.status_code}"


@pytest.mark.parametrize("method,path,body,_ok", ENDPOINTS)
def test_downstream_unreachable_502(gov_client, gov_cookie, gov_unreachable, method, path, body, _ok) -> None:
    resp = _call(gov_client, method, path, body, cookies=gov_cookie)
    assert resp.status_code == 502, f"{method} {path} → {resp.status_code}"


# ──────────────────────────────────────────────────────────────────────────────
# Upstream targeting — each router hits its own configured downstream URL
# ──────────────────────────────────────────────────────────────────────────────

def test_notification_hits_notification_url(gov_client, gov_cookie, gov_allowed) -> None:
    gov_client.get(PREFIX + "/admin/notification/templates", cookies=gov_cookie)
    assert any(u == f"{NOTIF_URL}/templates" for (_, u, _, _) in gov_allowed.requests)


def test_api_platform_hits_api_platform_url(gov_client, gov_cookie, gov_allowed) -> None:
    gov_client.get(PREFIX + "/admin/api-platform/keys", cookies=gov_cookie)
    assert any(u == f"{APIPLAT_URL}/keys" for (_, u, _, _) in gov_allowed.requests)


def test_pam_hits_pam_url(gov_client, gov_cookie, gov_allowed) -> None:
    gov_client.get(PREFIX + "/admin/pam/sessions", cookies=gov_cookie)
    assert any(u == f"{PAM_URL}/sessions" for (_, u, _, _) in gov_allowed.requests)


def test_dlp_hits_dlp_url(gov_client, gov_cookie, gov_allowed) -> None:
    gov_client.post(
        PREFIX + "/admin/dlp/scan", json={"content": "c"}, cookies=gov_cookie,
    )
    assert any(u == f"{DLP_URL}/core/scan" for (_, u, _, _) in gov_allowed.requests)


@pytest.mark.parametrize(
    "method,path,body,_ok",
    [e for e in ENDPOINTS if e[1].startswith("/admin/dlp/")],
)
def test_only_classify_is_sent_with_admin_authority(
    gov_client, gov_cookie, gov_allowed, method, path, body, _ok,
) -> None:
    """foundation-dlp lets only SERVICE/ADMIN actors write labels.

    Classify needs that authority.  Scan must not have it: it checks text the
    admin pastes in, and with it a hit would label the resource (or the
    ``adhoc-scan`` placeholder).  An ``X-Actor`` sent by the browser is never
    passed on.
    """
    _call(gov_client, method, path, body, cookies=gov_cookie, headers={"X-Actor": "SERVICE"})

    upstream = path.split("?")[0].removeprefix("/admin/dlp/")
    sent = [h for (_, u, h, _) in gov_allowed.requests if u == f"{DLP_URL}/core/{upstream}"]
    assert sent, path
    expected = "ADMIN" if upstream == "classification:tag" else None
    assert sent[-1].get("X-Actor") == expected


def test_compliance_hits_compliance_url(gov_client, gov_cookie, gov_allowed) -> None:
    gov_client.get(PREFIX + "/admin/compliance/ropa", cookies=gov_cookie)
    assert any(u == f"{COMPL_URL}/ropa" for (_, u, _, _) in gov_allowed.requests)


def test_dlp_classification_get_forwards_resource_params(gov_client, gov_cookie, gov_allowed) -> None:
    gov_client.get(
        PREFIX + "/admin/dlp/classification:get?resource_type=doc&resource_id=r9",
        cookies=gov_cookie,
    )
    calls = [
        (u, p) for (_, u, _, p) in gov_allowed.requests
        if "classification:get" in u
    ]
    assert calls
    _, params = calls[-1]
    assert params.get("resource_type") == "doc"
    assert params.get("resource_id") == "r9"


# ──────────────────────────────────────────────────────────────────────────────
# Cross-tenant override
# ──────────────────────────────────────────────────────────────────────────────

def test_tenant_id_override_changes_header(gov_client, gov_cookie, gov_allowed) -> None:
    target = "tnt_OTHER0000000000000000000001"
    gov_client.get(
        PREFIX + f"/admin/pam/sessions?tenant_id={target}", cookies=gov_cookie,
    )
    calls = [h for (_, u, h, _) in gov_allowed.requests if u == f"{PAM_URL}/sessions"]
    assert calls and calls[-1].get("X-Tenant-Id") == target


def test_tenant_id_absent_uses_caller_tenant(gov_client, gov_cookie, gov_allowed) -> None:
    gov_client.get(PREFIX + "/admin/pam/sessions", cookies=gov_cookie)
    calls = [h for (_, u, h, _) in gov_allowed.requests if u == f"{PAM_URL}/sessions"]
    assert calls and calls[-1].get("X-Tenant-Id") == CALLER_TENANT


def test_tenant_id_not_leaked_as_param_on_param_forwarding_route(
    gov_client, gov_cookie, gov_allowed,
) -> None:
    """tenant_id override must not leak into a downstream that forwards params."""
    target = "tnt_OTHER0000000000000000000001"
    gov_client.get(
        PREFIX + f"/admin/dlp/classification:get?resource_type=x&resource_id=r&tenant_id={target}",
        cookies=gov_cookie,
    )
    calls = [
        (h, p) for (_, u, h, p) in gov_allowed.requests
        if "classification:get" in u
    ]
    assert calls
    headers, params = calls[-1]
    assert headers.get("X-Tenant-Id") == target
    assert "tenant_id" not in params
