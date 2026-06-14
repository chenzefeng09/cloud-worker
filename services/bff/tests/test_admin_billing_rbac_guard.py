"""Tests: BFF ``require_tenant_rbac_bff`` guard on the admin_billing surface.

foundation-billing performs no per-user authorization of its own, so the BFF
guard is the authoritative enforcement point for subscription/quota visibility.
This round delegates only *read-only* surfaces to tenant administrators while
keeping every mutation (and the cross-tenant lane) reserved for the platform
super_admin:

  • Read (plans / subscription / usage / composite quota) → ``quota.read``
  • Write (create subscription / change-plan / cancel / create plan) →
    super_admin only (``platform.admin``)

The guard runs two fail-closed lanes against authz ``/check``:

  • Same-tenant: authorize the endpoint's action (``quota.read`` for reads)
    against the caller's own tenant.
  • Cross-tenant (``?tenant_id=`` ≠ own): require ``platform.admin``.

These tests drive the guard through the real billing proxy routes with an
action-aware authz stub, asserting:

  1. tenant_admin (quota.read) reads plans / subscription / usage / quota → 200
  2. member (no quota.read) → 403 on every read, upstream never called
  3. quota.read does NOT open the write surface — subscription mutations and
     plan creation still demand super_admin (platform.admin), so a holder of
     only quota.read is 403'd on writes
  4. super_admin (platform.admin via ``*``) retains full write access
  5. cross-tenant read + no platform.admin → 403 (tenant_admin confined)
  6. cross-tenant read + platform.admin (super_admin) → 200, foreign tenant
     forwarded on the composite quota call
  7. authz /check unreachable → fail-closed 403
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
OWN_TENANT = "tnt_01J5TEST00000000000000TNT1"  # matches authed_cookie's tenant
OTHER_TENANT = "tnt_OTHER0000000000000000001"


@pytest.fixture
def billing_client(settings: Settings) -> Iterator[TestClient]:
    """App with the admin_billing router included (it is in create_app already,
    but include it explicitly so this module is self-contained)."""
    app = create_app(settings)
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


class FakeBilling:
    """Action-aware authz /check stub + upstream foundation-billing responses.

    ``granted`` is the set of actions /check returns allowed=True for.  Every
    other action → allowed=False.  Records the action of each /check, the
    forwarded X-Tenant-Id header, and every upstream (non-check) call so tests
    can assert lane selection + the cross-tenant override.
    """

    def __init__(self, *, granted: set[str]) -> None:
        self.granted = granted
        self.check_actions: list[str] = []
        self.check_tenants: list[str] = []
        self.upstream_calls: list[tuple[str, str]] = []
        self.upstream_tenants: list[str] = []

    def route(self, method: str, url: str, **kw: Any) -> httpx.Response:
        if url.endswith("/check"):
            body = kw.get("json") or {}
            action = body.get("action", "")
            self.check_actions.append(action)
            self.check_tenants.append((kw.get("headers") or {}).get("X-Tenant-Id", ""))
            return httpx.Response(200, json={"allowed": action in self.granted})

        # Upstream foundation-billing REST mirror.
        self.upstream_calls.append((method, url))
        self.upstream_tenants.append((kw.get("headers") or {}).get("X-Tenant-Id", ""))
        if url.endswith("/subscriptions/current"):
            return httpx.Response(200, json={
                "id": "sub_1", "tenant_id": OWN_TENANT,
                "plan_id": "plan_pro", "status": "ACTIVE",
            })
        if url.endswith("/plans") and method == "GET":
            return httpx.Response(200, json={"items": [
                {"id": "plan_pro", "name": "Pro", "tier": "PRO", "quotas": {"sessions": 100}},
            ]})
        if url.endswith("/plans") and method == "POST":
            return httpx.Response(201, json={"id": "plan_new", "name": "New"})
        if url.endswith("/quota/usage"):
            return httpx.Response(200, json={"usage": {"sessions": 12}})
        if url.endswith("/subscriptions") and method == "POST":
            return httpx.Response(201, json={"id": "sub_new"})
        if url.endswith("/subscriptions/change-plan"):
            return httpx.Response(200, json={"id": "sub_1", "plan_id": "plan_pro"})
        if url.endswith("/subscriptions/cancel"):
            return httpx.Response(200, json={"id": "sub_1", "status": "CANCELED"})
        return httpx.Response(200, json={"ok": True})


def _install(fake: FakeBilling, monkeypatch: pytest.MonkeyPatch) -> FakeBilling:
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


# ── Same-tenant lane: tenant_admin (quota.read) reads ─────────────────────────

def test_tenant_admin_can_read_plans(
    billing_client: TestClient, authed_cookie: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """tenant_admin holding quota.read → GET plans proxied (200)."""
    fake = _install(FakeBilling(granted={"quota.read"}), monkeypatch)
    resp = billing_client.get(PREFIX + "/admin/billing/plans", cookies=authed_cookie)
    assert resp.status_code == 200
    assert fake.check_actions == ["quota.read"]
    assert fake.upstream_calls and "/plans" in fake.upstream_calls[0][1]


def test_tenant_admin_can_read_subscription(
    billing_client: TestClient, authed_cookie: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GET subscription gates on quota.read."""
    fake = _install(FakeBilling(granted={"quota.read"}), monkeypatch)
    resp = billing_client.get(
        PREFIX + "/admin/billing/subscription", cookies=authed_cookie,
    )
    assert resp.status_code == 200
    assert fake.check_actions == ["quota.read"]


def test_tenant_admin_can_read_usage(
    billing_client: TestClient, authed_cookie: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GET usage gates on quota.read."""
    fake = _install(FakeBilling(granted={"quota.read"}), monkeypatch)
    resp = billing_client.get(PREFIX + "/admin/billing/usage", cookies=authed_cookie)
    assert resp.status_code == 200
    assert fake.check_actions == ["quota.read"]


def test_tenant_admin_can_read_composite_quota_own_tenant(
    billing_client: TestClient, authed_cookie: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Composite quota endpoint with ?tenant_id=<own> stays same-tenant (quota.read)."""
    fake = _install(FakeBilling(granted={"quota.read"}), monkeypatch)
    resp = billing_client.get(
        PREFIX + f"/admin/billing/quota?tenant_id={OWN_TENANT}", cookies=authed_cookie,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["tenant_id"] == OWN_TENANT
    assert body["plan"]["limits"] == {"sessions": 100}
    assert body["usage"] == {"sessions": 12}
    # Same-tenant lane → exactly one quota.read check, no platform.admin probe.
    assert fake.check_actions == ["quota.read"]


# ── Same-tenant lane: member denied on reads ──────────────────────────────────

def test_member_cannot_read_plans(
    billing_client: TestClient, authed_cookie: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A member lacking quota.read → 403, upstream never called."""
    fake = _install(FakeBilling(granted=set()), monkeypatch)
    resp = billing_client.get(PREFIX + "/admin/billing/plans", cookies=authed_cookie)
    assert resp.status_code == 403
    assert fake.check_actions == ["quota.read"]
    assert fake.upstream_calls == []


def test_member_cannot_read_subscription(
    billing_client: TestClient, authed_cookie: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _install(FakeBilling(granted=set()), monkeypatch)
    resp = billing_client.get(
        PREFIX + "/admin/billing/subscription", cookies=authed_cookie,
    )
    assert resp.status_code == 403
    assert fake.upstream_calls == []


def test_member_cannot_read_usage(
    billing_client: TestClient, authed_cookie: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _install(FakeBilling(granted=set()), monkeypatch)
    resp = billing_client.get(PREFIX + "/admin/billing/usage", cookies=authed_cookie)
    assert resp.status_code == 403
    assert fake.upstream_calls == []


def test_member_cannot_read_composite_quota(
    billing_client: TestClient, authed_cookie: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _install(FakeBilling(granted=set()), monkeypatch)
    resp = billing_client.get(
        PREFIX + f"/admin/billing/quota?tenant_id={OWN_TENANT}", cookies=authed_cookie,
    )
    assert resp.status_code == 403
    assert fake.check_actions == ["quota.read"]
    assert fake.upstream_calls == []


# ── Write surface stays super_admin-only (quota.read must NOT open it) ─────────

def test_quota_read_does_not_open_subscription_create(
    billing_client: TestClient, authed_cookie: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tenant_admin holding quota.read may NOT create a subscription.

    Write endpoints are gated by ``require_super_admin_bff`` (platform.admin);
    a caller granted only quota.read is denied.
    """
    fake = _install(FakeBilling(granted={"quota.read"}), monkeypatch)
    resp = billing_client.post(
        PREFIX + "/admin/billing/subscriptions",
        json={"plan_id": "plan_pro"},
        cookies=authed_cookie,
    )
    assert resp.status_code == 403
    # super_admin guard probes platform.admin (not quota.read).
    assert fake.check_actions == ["platform.admin"]
    assert fake.upstream_calls == []


def test_quota_read_does_not_open_change_plan(
    billing_client: TestClient, authed_cookie: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """change-plan is super_admin-only; quota.read holder → 403."""
    fake = _install(FakeBilling(granted={"quota.read"}), monkeypatch)
    resp = billing_client.post(
        PREFIX + "/admin/billing/subscription/change-plan",
        json={"new_plan_id": "plan_pro"},
        cookies=authed_cookie,
    )
    assert resp.status_code == 403
    assert fake.check_actions == ["platform.admin"]
    assert fake.upstream_calls == []


def test_quota_read_does_not_open_cancel(
    billing_client: TestClient, authed_cookie: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """cancel is super_admin-only; quota.read holder → 403."""
    fake = _install(FakeBilling(granted={"quota.read"}), monkeypatch)
    resp = billing_client.post(
        PREFIX + "/admin/billing/subscription/cancel", json={}, cookies=authed_cookie,
    )
    assert resp.status_code == 403
    assert fake.check_actions == ["platform.admin"]
    assert fake.upstream_calls == []


def test_quota_read_does_not_open_plan_create(
    billing_client: TestClient, authed_cookie: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Creating a plan in the catalogue is super_admin-only; quota.read → 403."""
    fake = _install(FakeBilling(granted={"quota.read"}), monkeypatch)
    resp = billing_client.post(
        PREFIX + "/admin/billing/plans",
        json={"name": "New", "tier": "PRO"},
        cookies=authed_cookie,
    )
    assert resp.status_code == 403
    assert fake.check_actions == ["platform.admin"]
    assert fake.upstream_calls == []


# ── super_admin retains full write access (no regression) ─────────────────────

def test_super_admin_can_create_subscription(
    billing_client: TestClient, authed_cookie: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """super_admin (platform.admin) keeps write access to subscription create."""
    fake = _install(FakeBilling(granted={"platform.admin"}), monkeypatch)
    resp = billing_client.post(
        PREFIX + "/admin/billing/subscriptions",
        json={"plan_id": "plan_pro"},
        cookies=authed_cookie,
    )
    assert resp.status_code == 201
    assert fake.check_actions == ["platform.admin"]
    assert fake.upstream_calls and fake.upstream_calls[0][0] == "POST"


def test_super_admin_can_change_plan(
    billing_client: TestClient, authed_cookie: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _install(FakeBilling(granted={"platform.admin"}), monkeypatch)
    resp = billing_client.post(
        PREFIX + "/admin/billing/subscription/change-plan",
        json={"new_plan_id": "plan_pro"},
        cookies=authed_cookie,
    )
    assert resp.status_code == 200
    assert fake.check_actions == ["platform.admin"]


def test_super_admin_can_create_plan(
    billing_client: TestClient, authed_cookie: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _install(FakeBilling(granted={"platform.admin"}), monkeypatch)
    resp = billing_client.post(
        PREFIX + "/admin/billing/plans",
        json={"name": "New", "tier": "PRO"},
        cookies=authed_cookie,
    )
    assert resp.status_code == 201
    assert fake.check_actions == ["platform.admin"]


# ── Cross-tenant lane on the read surface ─────────────────────────────────────

def test_cross_tenant_tenant_admin_denied(
    billing_client: TestClient, authed_cookie: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """tenant_admin (quota.read but no platform.admin) reading a foreign tenant → 403."""
    fake = _install(FakeBilling(granted={"quota.read"}), monkeypatch)
    resp = billing_client.get(
        PREFIX + f"/admin/billing/quota?tenant_id={OTHER_TENANT}", cookies=authed_cookie,
    )
    assert resp.status_code == 403
    # Cross-tenant lane probes platform.admin against the caller's OWN tenant.
    assert fake.check_actions == ["platform.admin"]
    assert fake.upstream_calls == []


def test_cross_tenant_super_admin_allowed_forwards_foreign_tenant(
    billing_client: TestClient, authed_cookie: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """super_admin (platform.admin) may read a foreign tenant's quota via ?tenant_id=."""
    fake = _install(FakeBilling(granted={"platform.admin"}), monkeypatch)
    resp = billing_client.get(
        PREFIX + f"/admin/billing/quota?tenant_id={OTHER_TENANT}", cookies=authed_cookie,
    )
    assert resp.status_code == 200
    # Only the platform.admin probe runs; the quota.read action is bypassed on
    # the cross-tenant lane.  The probe is against the caller's own tenant; the
    # upstream override targets the foreign tenant.
    assert fake.check_actions == ["platform.admin"]
    assert fake.check_tenants == [OWN_TENANT]
    assert fake.upstream_calls and any("/quota/usage" in u for _, u in fake.upstream_calls)
    assert set(fake.upstream_tenants) == {OTHER_TENANT}


# ── Fail-closed transport ─────────────────────────────────────────────────────

def test_authz_unreachable_is_403(
    billing_client: TestClient, monkeypatch: pytest.MonkeyPatch, authed_cookie: dict,
) -> None:
    """authz /check transport failure → fail-closed 403."""
    _install_unreachable(monkeypatch)
    resp = billing_client.get(PREFIX + "/admin/billing/plans", cookies=authed_cookie)
    assert resp.status_code == 403


def test_unauthenticated_rejected(billing_client: TestClient) -> None:
    """No cookie → 401/403 before any authz call."""
    resp = billing_client.get(PREFIX + "/admin/billing/plans")
    assert resp.status_code in (401, 403)
