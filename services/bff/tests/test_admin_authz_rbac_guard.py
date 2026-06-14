"""Tests: BFF ``require_tenant_rbac_bff`` guard on the admin_authz surface.

The guard delegates the RBAC admin endpoints (roles / bindings / permissions)
to *tenant administrators* while preserving super_admin's cross-tenant reach.
It runs two fail-closed lanes against authz ``/check``:

  • Same-tenant: authorize the endpoint's action (``authz.role.manage`` /
    ``authz.binding.manage``) against the caller's own tenant.
  • Cross-tenant (``?tenant_id=`` ≠ own): require ``platform.admin``.

These tests drive the guard through the real ``GET /admin/authz/roles`` (and
``/bindings``) routes with an action-aware authz stub, asserting:

  1. same-tenant action allowed → 200 (proxied)
  2. same-tenant action denied  → 403 (no upstream call)
  3. cross-tenant + platform.admin allowed → 200
  4. cross-tenant + no platform.admin     → 403
  5. authz /check unreachable → 403 (fail-closed)
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

PREFIX = "/api/bff/v1"
OWN_TENANT = "tnt_01J5TEST00000000000000TNT1"  # matches authed_cookie's tenant
OTHER_TENANT = "tnt_OTHER0000000000000000001"


class FakeAuthz:
    """Action-aware authz /check stub + upstream roles/bindings responses.

    ``granted`` is the set of actions /check returns allowed=True for.  Every
    other action → allowed=False.  Records the (action) of each /check and the
    forwarded X-Tenant-Id header so tests can assert lane selection + override.
    """

    def __init__(self, *, granted: set[str]) -> None:
        self.granted = granted
        self.check_actions: list[str] = []
        self.check_tenants: list[str] = []
        self.upstream_calls: list[tuple[str, str]] = []

    def route(self, method: str, url: str, **kw: Any) -> httpx.Response:
        if url.endswith("/check"):
            body = kw.get("json") or {}
            action = body.get("action", "")
            self.check_actions.append(action)
            self.check_tenants.append((kw.get("headers") or {}).get("X-Tenant-Id", ""))
            return httpx.Response(200, json={"allowed": action in self.granted})

        # Upstream authz REST mirror (roles / bindings).
        self.upstream_calls.append((method, url))
        if "/roles" in url:
            return httpx.Response(200, json={"items": [], "next_cursor": None})
        if "/bindings" in url:
            return httpx.Response(200, json={"items": [], "next_cursor": None})
        return httpx.Response(200, json={"ok": True})


def _install(fake: FakeAuthz, monkeypatch: pytest.MonkeyPatch) -> FakeAuthz:
    class _AC:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        async def get(self, url, **kw): return fake.route("GET", url, **kw)
        async def post(self, url, **kw): return fake.route("POST", url, **kw)
        async def patch(self, url, **kw): return fake.route("PATCH", url, **kw)
        async def delete(self, url, **kw): return fake.route("DELETE", url, **kw)

    monkeypatch.setattr(httpx, "AsyncClient", _AC)
    return fake


def _install_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    class _AC:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        async def get(self, url, **kw): raise httpx.ConnectError("authz down")
        async def post(self, url, **kw): raise httpx.ConnectError("authz down")
        async def patch(self, url, **kw): raise httpx.ConnectError("authz down")
        async def delete(self, url, **kw): raise httpx.ConnectError("authz down")

    monkeypatch.setattr(httpx, "AsyncClient", _AC)


# ── Same-tenant lane ──────────────────────────────────────────────────────────

def test_same_tenant_action_allowed_proxies(
    client: TestClient, authed_cookie: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """tenant_admin holding authz.role.manage → GET roles proxied (200)."""
    fake = _install(FakeAuthz(granted={"authz.role.manage"}), monkeypatch)
    resp = client.get(PREFIX + "/admin/authz/roles", cookies=authed_cookie)
    assert resp.status_code == 200
    # Same-tenant lane: exactly one /check for the endpoint's action, no
    # platform.admin probe, and the upstream roles call happened.
    assert fake.check_actions == ["authz.role.manage"]
    assert fake.upstream_calls and "/roles" in fake.upstream_calls[0][1]


def test_same_tenant_action_denied_403_no_upstream(
    client: TestClient, authed_cookie: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A member lacking authz.role.manage → 403, upstream never called."""
    fake = _install(FakeAuthz(granted=set()), monkeypatch)
    resp = client.get(PREFIX + "/admin/authz/roles", cookies=authed_cookie)
    assert resp.status_code == 403
    assert fake.check_actions == ["authz.role.manage"]
    assert fake.upstream_calls == []


def test_bindings_use_binding_manage_action(
    client: TestClient, authed_cookie: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Binding endpoints gate on authz.binding.manage (distinct from role)."""
    fake = _install(FakeAuthz(granted={"authz.binding.manage"}), monkeypatch)
    resp = client.get(PREFIX + "/admin/authz/bindings", cookies=authed_cookie)
    assert resp.status_code == 200
    assert fake.check_actions == ["authz.binding.manage"]

    # Holding only role.manage must NOT open the binding surface.
    fake2 = _install(FakeAuthz(granted={"authz.role.manage"}), monkeypatch)
    resp2 = client.get(PREFIX + "/admin/authz/bindings", cookies=authed_cookie)
    assert resp2.status_code == 403
    assert fake2.check_actions == ["authz.binding.manage"]


def test_same_tenant_explicit_own_tenant_id_is_not_cross_tenant(
    client: TestClient, authed_cookie: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """?tenant_id= equal to the caller's own tenant stays on the same-tenant lane."""
    fake = _install(FakeAuthz(granted={"authz.role.manage"}), monkeypatch)
    resp = client.get(
        PREFIX + f"/admin/authz/roles?tenant_id={OWN_TENANT}", cookies=authed_cookie,
    )
    assert resp.status_code == 200
    # No platform.admin probe — it's the same tenant.
    assert fake.check_actions == ["authz.role.manage"]


# ── Cross-tenant lane ─────────────────────────────────────────────────────────

def test_cross_tenant_requires_platform_admin_denied(
    client: TestClient, authed_cookie: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """tenant_admin (role.manage but no platform.admin) hitting a foreign tenant → 403."""
    fake = _install(FakeAuthz(granted={"authz.role.manage"}), monkeypatch)
    resp = client.get(
        PREFIX + f"/admin/authz/roles?tenant_id={OTHER_TENANT}", cookies=authed_cookie,
    )
    assert resp.status_code == 403
    # Cross-tenant lane probes platform.admin (against the caller's OWN tenant).
    assert fake.check_actions == ["platform.admin"]
    assert fake.upstream_calls == []


def test_cross_tenant_super_admin_allowed(
    client: TestClient, authed_cookie: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """super_admin (platform.admin) may manage a foreign tenant via ?tenant_id=."""
    fake = _install(FakeAuthz(granted={"platform.admin"}), monkeypatch)
    resp = client.get(
        PREFIX + f"/admin/authz/roles?tenant_id={OTHER_TENANT}", cookies=authed_cookie,
    )
    assert resp.status_code == 200
    # Only the platform.admin probe runs; the action check is bypassed on the
    # cross-tenant lane.  The override targets the foreign tenant upstream.
    assert fake.check_actions == ["platform.admin"]
    assert fake.check_tenants == [OWN_TENANT]  # probe is against own tenant
    assert fake.upstream_calls and "/roles" in fake.upstream_calls[0][1]


def test_cross_tenant_override_forwards_foreign_tenant_upstream(
    client: TestClient, authed_cookie: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After the platform.admin gate, the upstream call targets the foreign tenant."""
    captured: dict = {}

    fake = FakeAuthz(granted={"platform.admin"})
    orig_route = fake.route

    def route(method: str, url: str, **kw: Any) -> httpx.Response:
        if not url.endswith("/check") and "/roles" in url:
            captured["x_tenant"] = (kw.get("headers") or {}).get("X-Tenant-Id")
        return orig_route(method, url, **kw)

    fake.route = route  # type: ignore[method-assign]
    _install(fake, monkeypatch)

    resp = client.get(
        PREFIX + f"/admin/authz/roles?tenant_id={OTHER_TENANT}", cookies=authed_cookie,
    )
    assert resp.status_code == 200
    assert captured["x_tenant"] == OTHER_TENANT


# ── Fail-closed transport ─────────────────────────────────────────────────────

def test_authz_unreachable_is_403(
    client: TestClient, authed_cookie: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """authz /check transport failure → fail-closed 403."""
    _install_unreachable(monkeypatch)
    resp = client.get(PREFIX + "/admin/authz/roles", cookies=authed_cookie)
    assert resp.status_code == 403


def test_unauthenticated_rejected(client: TestClient) -> None:
    """No cookie → 401/403 before any authz call."""
    resp = client.get(PREFIX + "/admin/authz/roles")
    assert resp.status_code in (401, 403)
