"""Tests: BFF large-attachment admin proxy.

Covers for every endpoint: unauthenticated 401/403, authz-denied 403,
super_admin passthrough 2xx, downstream 4xx 透传, downstream unreachable 502,
cross-tenant ?tenant_id= header override.

Plus large-attachment specifics:
  * tenant scoping is forwarded via ``X-Tenant-Id`` (never the body),
  * the user-allows list is passed through verbatim, and a *bare array*
    upstream shape is tolerated.

Tenant delegation (``require_admin_module_bff("session.attachment.large.create")``):
  * a tenant_admin (holds ``session.attachment.large.create`` via ``session.*``)
    passes the same-tenant gate;
  * a plain member (holds only ``session.attachment.read`` /
    ``session.attachment.create``) is 403'd at the BFF;
  * a tenant_admin targeting a *foreign* ``?tenant_id=`` is 403'd by the
    cross-tenant lane (it lacks ``platform.admin``);
  * the tenant-delegated path forwards no ``X-Actor: ADMIN`` (only the static
    ``X-Foundation-Scopes`` grant lifecycle needs to admit the call).
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

LIFECYCLE_URL = "http://lifecycle-test/api/lifecycle/v1"
CALLER_TENANT = "tnt_01J5TEST00000000000000TNT1"


@pytest.fixture
def la_settings() -> Settings:
    return Settings(
        env="test",
        log_level="WARNING",
        cookie_secret=TEST_COOKIE_SECRET,
        cookie_secure=False,
        foundation_identity_url="http://identity-test/api/identity/v1",
        session_orchestrator_url="http://orch-test:8006/api/session/v1",
        foundation_lifecycle_url=LIFECYCLE_URL,
        cors_origins="http://localhost",
        otel_enabled=False,
    )


@pytest.fixture
def la_client(la_settings: Settings) -> Iterator[TestClient]:
    app = create_app(la_settings)
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture
def la_cookie(la_settings: Settings) -> dict[str, str]:
    from foundation_sdk import CookieSigner, CookieSignerSettings

    signer = CookieSigner(
        CookieSignerSettings(
            secret_key=la_settings.cookie_secret,
            salt="agent-bff-session",
            max_age_seconds=la_settings.cookie_max_age,
        ),
    )
    token = signer.sign(
        {
            "user_id": "usr_01J5TEST0000000000000USER1",
            "tenant_id": CALLER_TENANT,
            "access_token": "fake-jwt-for-test",
        },
    )
    return {la_settings.cookie_name: token}


class FakeLifecycle:
    def __init__(
        self,
        *,
        authz_allowed: bool = True,
        allowed_actions: set[str] | None = None,
        error_status: int | None = None,
        bare_array_allows: bool = False,
    ) -> None:
        self.authz_allowed = authz_allowed
        self.allowed_actions = allowed_actions
        self.error_status = error_status
        self.bare_array_allows = bare_array_allows
        self.checked_actions: list[str] = []
        self.requests: list[tuple[str, str, dict, dict]] = []

    def route(self, method: str, url: str, **kw: Any) -> httpx.Response:
        self.requests.append(
            (method, url, dict(kw.get("headers") or {}), dict(kw.get("params") or {})),
        )

        if url.endswith("/check"):
            action = (kw.get("json") or {}).get("action", "")
            self.checked_actions.append(action)
            if self.allowed_actions is not None:
                allowed = action in self.allowed_actions
            else:
                allowed = self.authz_allowed
            return httpx.Response(200, json={"allowed": allowed})

        if self.error_status is not None:
            return httpx.Response(
                self.error_status,
                json={"title": "upstream error", "status": self.error_status},
            )

        if url.endswith("/tenant-setting"):
            if method == "PUT":
                return httpx.Response(
                    200,
                    json={"tenant_id": CALLER_TENANT, "large_attachment_enabled": True},
                )
            return httpx.Response(
                200,
                json={"tenant_id": CALLER_TENANT, "large_attachment_enabled": False},
            )
        if url.endswith("/user-allows"):
            if method == "PUT":
                return httpx.Response(
                    200, json={"user_id": "usr_x", "allowed": True},
                )
            rows = [{"user_id": "usr_x", "allowed": True}]
            return httpx.Response(200, json=rows if self.bare_array_allows else {"items": rows})
        if "/user-allows/" in url:  # DELETE {user_id}
            return httpx.Response(200, json={"deleted": True, "user_id": "usr_x"})

        return httpx.Response(200, json={"ok": True})


def _install(fake: FakeLifecycle, monkeypatch: pytest.MonkeyPatch) -> FakeLifecycle:
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
def la_allowed(monkeypatch: pytest.MonkeyPatch) -> FakeLifecycle:
    return _install(FakeLifecycle(authz_allowed=True), monkeypatch)


@pytest.fixture
def la_denied(monkeypatch: pytest.MonkeyPatch) -> FakeLifecycle:
    return _install(FakeLifecycle(authz_allowed=False), monkeypatch)


@pytest.fixture
def la_404(monkeypatch: pytest.MonkeyPatch) -> FakeLifecycle:
    return _install(FakeLifecycle(authz_allowed=True, error_status=404), monkeypatch)


@pytest.fixture
def la_unreachable(monkeypatch: pytest.MonkeyPatch) -> FakeLifecycle:
    fake = FakeLifecycle(authz_allowed=True)

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


# The ``member`` system role's session-attachment grants are
# ``session.attachment.read`` / ``session.attachment.create`` — crucially NOT
# ``session.attachment.large.create`` (the BFF gate action).
_MEMBER_ACTIONS = {
    "session.attachment.read",
    "session.attachment.create",
}

# tenant_admin holds ``session.attachment.large.create`` (via the ``session.*``
# wildcard) but NOT ``platform.admin`` — passes the same-tenant gate, barred
# from cross-tenant operations.
_TENANT_ADMIN_ACTIONS = {"session.attachment.large.create"}

# The single action the BFF guard authorizes for every large-attachment endpoint.
_GATE_ACTION = "session.attachment.large.create"


@pytest.fixture
def la_member(monkeypatch: pytest.MonkeyPatch) -> FakeLifecycle:
    return _install(FakeLifecycle(allowed_actions=_MEMBER_ACTIONS), monkeypatch)


@pytest.fixture
def la_tenant_admin(monkeypatch: pytest.MonkeyPatch) -> FakeLifecycle:
    return _install(FakeLifecycle(allowed_actions=_TENANT_ADMIN_ACTIONS), monkeypatch)


# (method, path, json-body, happy-status)
ENDPOINTS = [
    ("GET",    "/admin/large-attachment/tenant-setting", None, 200),
    ("PUT",    "/admin/large-attachment/tenant-setting", {"enabled": True}, 200),
    ("GET",    "/admin/large-attachment/user-allows", None, 200),
    ("PUT",    "/admin/large-attachment/user-allows", {"user_id": "usr_x", "allowed": True}, 200),
    ("DELETE", "/admin/large-attachment/user-allows/usr_x", None, 200),
]


def _call(client: TestClient, method: str, path: str, body, **kw):
    fn = getattr(client, method.lower())
    if body is not None and method in ("POST", "PUT", "PATCH"):
        return fn(PREFIX + path, json=body, **kw)
    return fn(PREFIX + path, **kw)


@pytest.mark.parametrize("method,path,body,_ok", ENDPOINTS)
def test_unauthenticated_rejected(la_client, method, path, body, _ok) -> None:
    resp = _call(la_client, method, path, body)
    assert resp.status_code in (401, 403), f"{method} {path} → {resp.status_code}"


@pytest.mark.parametrize("method,path,body,_ok", ENDPOINTS)
def test_authz_denied_403(la_client, la_cookie, la_denied, method, path, body, _ok) -> None:
    resp = _call(la_client, method, path, body, cookies=la_cookie)
    assert resp.status_code == 403, f"{method} {path} → {resp.status_code}"


@pytest.mark.parametrize("method,path,body,ok", ENDPOINTS)
def test_happy_path_passthrough(la_client, la_cookie, la_allowed, method, path, body, ok) -> None:
    resp = _call(la_client, method, path, body, cookies=la_cookie)
    assert resp.status_code == ok, f"{method} {path} → {resp.status_code} ({resp.text})"


@pytest.mark.parametrize("method,path,body,_ok", ENDPOINTS)
def test_downstream_4xx_passthrough(la_client, la_cookie, la_404, method, path, body, _ok) -> None:
    resp = _call(la_client, method, path, body, cookies=la_cookie)
    assert resp.status_code == 404, f"{method} {path} → {resp.status_code}"


@pytest.mark.parametrize("method,path,body,_ok", ENDPOINTS)
def test_downstream_unreachable_502(la_client, la_cookie, la_unreachable, method, path, body, _ok) -> None:
    resp = _call(la_client, method, path, body, cookies=la_cookie)
    assert resp.status_code == 502, f"{method} {path} → {resp.status_code}"


def test_hits_lifecycle_url(la_client, la_cookie, la_allowed) -> None:
    la_client.get(PREFIX + "/admin/large-attachment/tenant-setting", cookies=la_cookie)
    assert any(
        u == f"{LIFECYCLE_URL}/large-attachment/tenant-setting"
        for (_, u, _, _) in la_allowed.requests
    )


def test_put_body_forwarded(la_client, la_cookie, la_allowed) -> None:
    """PUT tenant-setting forwards {enabled} verbatim; tenant goes via header."""
    la_client.put(
        PREFIX + "/admin/large-attachment/tenant-setting",
        json={"enabled": True},
        cookies=la_cookie,
    )
    puts = [
        (u, h) for (m, u, h, _) in la_allowed.requests
        if m == "PUT" and u.endswith("/tenant-setting")
    ]
    assert puts
    url, headers = puts[-1]
    assert url == f"{LIFECYCLE_URL}/large-attachment/tenant-setting"
    assert headers.get("X-Tenant-Id") == CALLER_TENANT


def test_user_allows_items_envelope(la_client, la_cookie, la_allowed) -> None:
    resp = la_client.get(
        PREFIX + "/admin/large-attachment/user-allows", cookies=la_cookie,
    )
    assert resp.status_code == 200
    assert resp.json() == {"items": [{"user_id": "usr_x", "allowed": True}]}


def test_user_allows_bare_array_passthrough(la_client, la_cookie, monkeypatch) -> None:
    """A bare-array upstream shape is tolerated and passed through verbatim."""
    _install(FakeLifecycle(authz_allowed=True, bare_array_allows=True), monkeypatch)
    resp = la_client.get(
        PREFIX + "/admin/large-attachment/user-allows", cookies=la_cookie,
    )
    assert resp.status_code == 200
    assert resp.json() == [{"user_id": "usr_x", "allowed": True}]


def test_tenant_id_override_changes_header(la_client, la_cookie, la_allowed) -> None:
    target = "tnt_OTHER0000000000000000000001"
    la_client.get(
        PREFIX + f"/admin/large-attachment/tenant-setting?tenant_id={target}",
        cookies=la_cookie,
    )
    calls = [
        h for (_, u, h, _) in la_allowed.requests
        if u == f"{LIFECYCLE_URL}/large-attachment/tenant-setting"
    ]
    assert calls and calls[-1].get("X-Tenant-Id") == target


def test_tenant_id_absent_uses_caller_tenant(la_client, la_cookie, la_allowed) -> None:
    la_client.get(
        PREFIX + "/admin/large-attachment/tenant-setting", cookies=la_cookie,
    )
    calls = [
        h for (_, u, h, _) in la_allowed.requests
        if u == f"{LIFECYCLE_URL}/large-attachment/tenant-setting"
    ]
    assert calls and calls[-1].get("X-Tenant-Id") == CALLER_TENANT


def test_user_id_forwarded(la_client, la_cookie, la_allowed) -> None:
    la_client.get(
        PREFIX + "/admin/large-attachment/tenant-setting", cookies=la_cookie,
    )
    calls = [
        h for (_, u, h, _) in la_allowed.requests
        if u == f"{LIFECYCLE_URL}/large-attachment/tenant-setting"
    ]
    assert calls and calls[-1].get("X-User-Id") == "usr_01J5TEST0000000000000USER1"


def test_delete_hits_user_path(la_client, la_cookie, la_allowed) -> None:
    la_client.delete(
        PREFIX + "/admin/large-attachment/user-allows/usr_ABC", cookies=la_cookie,
    )
    assert any(
        u == f"{LIFECYCLE_URL}/large-attachment/user-allows/usr_ABC"
        for (m, u, _, _) in la_allowed.requests if m == "DELETE"
    )


# ──────────────────────────────────────────────────────────────────────────────
# Tenant delegation — gated on ``session.attachment.large.create`` (held by
# tenant_admin via ``session.*``, not by member).
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("method,path,body,ok", ENDPOINTS)
def test_tenant_admin_allowed(
    la_client, la_cookie, la_tenant_admin, method, path, body, ok,
) -> None:
    """tenant_admin (holds session.attachment.large.create, no platform.admin)."""
    resp = _call(la_client, method, path, body, cookies=la_cookie)
    assert resp.status_code == ok, f"{method} {path} → {resp.status_code} ({resp.text})"
    assert _GATE_ACTION in la_tenant_admin.checked_actions


@pytest.mark.parametrize("method,path,body,_ok", ENDPOINTS)
def test_member_denied(
    la_client, la_cookie, la_member, method, path, body, _ok,
) -> None:
    """A plain member (no session.attachment.large.create) is 403'd at the BFF."""
    resp = _call(la_client, method, path, body, cookies=la_cookie)
    assert resp.status_code == 403, f"{method} {path} → {resp.status_code}"


@pytest.mark.parametrize("method,path,body,_ok", ENDPOINTS)
def test_no_admin_actor_forwarded(
    la_client, la_cookie, la_tenant_admin, method, path, body, _ok,
) -> None:
    """The tenant-delegated path forwards no X-Actor:ADMIN (only the static
    X-Foundation-Scopes grant lifecycle needs to admit the call)."""
    resp = _call(la_client, method, path, body, cookies=la_cookie)
    assert resp.status_code in (200, 201)
    forwarded = [
        h for (_, u, h, _) in la_tenant_admin.requests if not u.endswith("/check")
    ]
    assert forwarded, "no upstream call recorded"
    for hdrs in forwarded:
        assert "X-Actor" not in hdrs, f"{method} {path} leaked X-Actor: {hdrs}"


def test_tenant_admin_cross_tenant_forbidden(
    la_client, la_cookie, la_tenant_admin,
) -> None:
    """tenant_admin (no platform.admin) targeting a foreign tenant → 403, and no
    lifecycle call is forwarded."""
    foreign = "tnt_OTHER0000000000000000000001"
    resp = la_client.get(
        PREFIX + f"/admin/large-attachment/tenant-setting?tenant_id={foreign}",
        cookies=la_cookie,
    )
    assert resp.status_code == 403
    assert not any(
        u.startswith(f"{LIFECYCLE_URL}/large-attachment")
        for (_, u, _, _) in la_tenant_admin.requests
    )


def test_super_admin_cross_tenant_allowed(
    la_client, la_cookie, la_allowed,
) -> None:
    """super_admin (platform.admin via authz_allowed) may target any tenant."""
    target = "tnt_OTHER0000000000000000000001"
    resp = la_client.get(
        PREFIX + f"/admin/large-attachment/tenant-setting?tenant_id={target}",
        cookies=la_cookie,
    )
    assert resp.status_code == 200
    calls = [
        h for (_, u, h, _) in la_allowed.requests
        if u == f"{LIFECYCLE_URL}/large-attachment/tenant-setting"
    ]
    assert calls and calls[-1].get("X-Tenant-Id") == target
