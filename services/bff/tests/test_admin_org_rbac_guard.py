"""Tests: BFF ``require_tenant_rbac_bff`` guard on the admin_org surface.

foundation-org performs no per-user authorization of its own, so the BFF guard
is the authoritative enforcement point for the org structure surface.  It
delegates to *tenant administrators* while preserving super_admin's
cross-tenant reach, running two fail-closed lanes against authz ``/check``:

  • Same-tenant: authorize the endpoint's action (``org.read`` for reads /
    ``org.manage`` for writes) against the caller's own tenant.
  • Cross-tenant (``?tenant_id=`` ≠ own): require ``platform.admin``.

These tests drive the guard through the real org proxy routes with an
action-aware authz stub, asserting:

  1. tenant_admin (org.read + org.manage) reads and writes → 200/201/proxied
  2. member (neither token) → 403, upstream never called
  3. read endpoint gates on org.read (org.manage alone is not enough)
  4. cross-tenant + no platform.admin → 403
  5. cross-tenant + platform.admin (super_admin) → 200, foreign tenant forwarded
  6. authz /check unreachable → fail-closed 403
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

PREFIX = "/api/bff/v1"
OWN_TENANT = "tnt_01J5TEST00000000000000TNT1"  # matches authed_cookie's tenant
OTHER_TENANT = "tnt_OTHER0000000000000000001"


class FakeOrg:
    """Action-aware authz /check stub + upstream org responses.

    ``granted`` is the set of actions /check returns allowed=True for.  Every
    other action → allowed=False.  Records the action of each /check, the
    forwarded X-Tenant-Id header, and every upstream (non-check) call so tests
    can assert lane selection + override.
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

        # Upstream foundation-org REST mirror.
        self.upstream_calls.append((method, url))
        self.upstream_tenants.append((kw.get("headers") or {}).get("X-Tenant-Id", ""))
        if url.endswith("/archive"):
            # delete_org_unit archives before deleting; return the unit unchanged.
            return httpx.Response(200, json={"id": "ou_1", "status": "archived"})
        if url.endswith("/members") and method == "GET":
            # list_team_members upstream returns a bare array.
            return httpx.Response(200, json=[])
        if url.endswith("/members") and method == "POST":
            # add_team_member upstream returns the created member object.
            return httpx.Response(201, json={"team_id": "team_1", "user_id": "usr_x"})
        if "/org-units" in url or "/teams" in url:
            return httpx.Response(200, json={"items": [], "next_cursor": None})
        return httpx.Response(200, json={"ok": True})


def _install(fake: FakeOrg, monkeypatch: pytest.MonkeyPatch) -> FakeOrg:
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


# ── Same-tenant lane: tenant_admin reads + writes ─────────────────────────────

def test_tenant_admin_can_read_org_units(
    client: TestClient, authed_cookie: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """tenant_admin holding org.read → GET org-units proxied (200)."""
    fake = _install(FakeOrg(granted={"org.read", "org.manage"}), monkeypatch)
    resp = client.get(PREFIX + "/admin/org/org-units", cookies=authed_cookie)
    assert resp.status_code == 200
    # Exactly one /check for the read action, then the upstream call happened.
    assert fake.check_actions == ["org.read"]
    assert fake.upstream_calls and "/org-units" in fake.upstream_calls[0][1]


def test_tenant_admin_can_read_team_members(
    client: TestClient, authed_cookie: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GET teams/{id}/members gates on org.read."""
    fake = _install(FakeOrg(granted={"org.read"}), monkeypatch)
    resp = client.get(
        PREFIX + "/admin/org/teams/team_1/members", cookies=authed_cookie,
    )
    assert resp.status_code == 200
    assert fake.check_actions == ["org.read"]


def test_tenant_admin_can_create_org_unit(
    client: TestClient, authed_cookie: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """tenant_admin holding org.manage → POST org-units proxied (201)."""
    fake = _install(FakeOrg(granted={"org.read", "org.manage"}), monkeypatch)
    resp = client.post(
        PREFIX + "/admin/org/org-units",
        json={"name": "Eng", "parent_id": None},
        cookies=authed_cookie,
    )
    assert resp.status_code == 201
    assert fake.check_actions == ["org.manage"]
    assert fake.upstream_calls and fake.upstream_calls[0][0] == "POST"


def test_tenant_admin_can_add_team_member(
    client: TestClient, authed_cookie: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POST teams/{id}/members gates on org.manage."""
    fake = _install(FakeOrg(granted={"org.manage"}), monkeypatch)
    resp = client.post(
        PREFIX + "/admin/org/teams/team_1/members",
        json={"user_id": "usr_x", "role_in_team": "member"},
        cookies=authed_cookie,
    )
    assert resp.status_code == 201
    assert fake.check_actions == ["org.manage"]


def test_tenant_admin_can_delete_org_unit_archive_then_delete(
    client: TestClient, authed_cookie: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DELETE org-units gates on org.manage and runs archive + delete upstream."""
    fake = _install(FakeOrg(granted={"org.manage"}), monkeypatch)
    resp = client.delete(
        PREFIX + "/admin/org/org-units/ou_1", cookies=authed_cookie,
    )
    assert resp.status_code == 200
    assert fake.check_actions == ["org.manage"]
    # archive POST then DELETE both ran upstream.
    methods = [m for m, _ in fake.upstream_calls]
    assert methods == ["POST", "DELETE"]


# ── Same-tenant lane: member denied ───────────────────────────────────────────

def test_member_cannot_read_org_units(
    client: TestClient, authed_cookie: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A member lacking org.read → 403, upstream never called."""
    fake = _install(FakeOrg(granted=set()), monkeypatch)
    resp = client.get(PREFIX + "/admin/org/org-units", cookies=authed_cookie)
    assert resp.status_code == 403
    assert fake.check_actions == ["org.read"]
    assert fake.upstream_calls == []


def test_member_cannot_write_org_units(
    client: TestClient, authed_cookie: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A member lacking org.manage → 403 on a write, upstream never called."""
    fake = _install(FakeOrg(granted=set()), monkeypatch)
    resp = client.post(
        PREFIX + "/admin/org/org-units",
        json={"name": "Eng"},
        cookies=authed_cookie,
    )
    assert resp.status_code == 403
    assert fake.check_actions == ["org.manage"]
    assert fake.upstream_calls == []


def test_read_token_does_not_open_write_surface(
    client: TestClient, authed_cookie: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Holding only org.read must NOT authorize a write (org.manage)."""
    fake = _install(FakeOrg(granted={"org.read"}), monkeypatch)
    resp = client.post(
        PREFIX + "/admin/org/teams",
        json={"name": "Team A"},
        cookies=authed_cookie,
    )
    assert resp.status_code == 403
    assert fake.check_actions == ["org.manage"]
    assert fake.upstream_calls == []


# ── Cross-tenant lane ─────────────────────────────────────────────────────────

def test_cross_tenant_tenant_admin_denied(
    client: TestClient, authed_cookie: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """tenant_admin (org.* but no platform.admin) hitting a foreign tenant → 403."""
    fake = _install(FakeOrg(granted={"org.read", "org.manage"}), monkeypatch)
    resp = client.get(
        PREFIX + f"/admin/org/org-units?tenant_id={OTHER_TENANT}",
        cookies=authed_cookie,
    )
    assert resp.status_code == 403
    # Cross-tenant lane probes platform.admin (against the caller's OWN tenant).
    assert fake.check_actions == ["platform.admin"]
    assert fake.upstream_calls == []


def test_cross_tenant_super_admin_allowed_forwards_foreign_tenant(
    client: TestClient, authed_cookie: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """super_admin (platform.admin) may read a foreign tenant via ?tenant_id=."""
    fake = _install(FakeOrg(granted={"platform.admin"}), monkeypatch)
    resp = client.get(
        PREFIX + f"/admin/org/org-units?tenant_id={OTHER_TENANT}",
        cookies=authed_cookie,
    )
    assert resp.status_code == 200
    # Only the platform.admin probe runs; the org.read action check is bypassed
    # on the cross-tenant lane.  The probe is against the caller's own tenant,
    # while the upstream override targets the foreign tenant.
    assert fake.check_actions == ["platform.admin"]
    assert fake.check_tenants == [OWN_TENANT]
    assert fake.upstream_calls and "/org-units" in fake.upstream_calls[0][1]
    assert fake.upstream_tenants == [OTHER_TENANT]


def test_same_tenant_explicit_own_tenant_id_is_not_cross_tenant(
    client: TestClient, authed_cookie: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """?tenant_id= equal to the caller's own tenant stays on the same-tenant lane."""
    fake = _install(FakeOrg(granted={"org.read"}), monkeypatch)
    resp = client.get(
        PREFIX + f"/admin/org/org-units?tenant_id={OWN_TENANT}",
        cookies=authed_cookie,
    )
    assert resp.status_code == 200
    assert fake.check_actions == ["org.read"]


# ── Fail-closed transport ─────────────────────────────────────────────────────

def test_authz_unreachable_is_403(
    client: TestClient, authed_cookie: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """authz /check transport failure → fail-closed 403."""
    _install_unreachable(monkeypatch)
    resp = client.get(PREFIX + "/admin/org/org-units", cookies=authed_cookie)
    assert resp.status_code == 403


def test_unauthenticated_rejected(client: TestClient) -> None:
    """No cookie → 401/403 before any authz call."""
    resp = client.get(PREFIX + "/admin/org/org-units")
    assert resp.status_code in (401, 403)
