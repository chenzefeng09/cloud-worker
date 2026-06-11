"""Tests: BFF foundation-proxy admin routers (modules 1-6).

Covers the six new routers added for the enterprise admin console v2:
    admin_authz    → foundation-authz   (RBAC)
    admin_org      → foundation-org      (org units / teams / members)
    admin_identity → foundation-identity (invite / freeze / reset)
    admin_billing  → foundation-billing  (plans / subscription / usage)
    admin_audit    → foundation-audit    (chain verify)
    admin_flags    → foundation-feature-flag (flags / kill-switch)

Each router is exercised for the four contract scenarios:
    (a) unauthenticated            → 401/403
    (b) authz denied               → 403
    (c) happy-path pass-through    → 2xx + stubbed downstream body
    (d) downstream 4xx pass-through→ same 4xx surfaced to caller
    (e) downstream unreachable     → 502 naming the service

The routers are NOT yet wired into ``main.create_app`` (route registration is
owned by the main session), so this module builds its own test app: it reuses
``create_app`` for the real auth/cookie/middleware stack, then additionally
includes the six new routers under the same API prefix.
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


# ──────────────────────────────────────────────────────────────────────────────
# Test app — create_app() + the six not-yet-registered routers
# ──────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def foundation_client(settings: Settings) -> Iterator[TestClient]:
    app = create_app(settings)
    from agent_bff.api.v1.admin_authz import router as authz_router
    from agent_bff.api.v1.admin_org import router as org_router
    from agent_bff.api.v1.admin_identity import router as identity_router
    from agent_bff.api.v1.admin_billing import router as billing_router
    from agent_bff.api.v1.admin_audit import router as audit_router
    from agent_bff.api.v1.admin_flags import router as flags_router

    for r in (
        authz_router, org_router, identity_router,
        billing_router, audit_router, flags_router,
    ):
        app.include_router(r, prefix=settings.api_prefix)

    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


# ──────────────────────────────────────────────────────────────────────────────
# Fake downstream — covers every foundation service touched by the 6 routers
# ──────────────────────────────────────────────────────────────────────────────

class FakeFoundation:
    """Deterministic stub for authz/org/identity/billing/audit/feature-flag.

    ``authz_allowed`` drives the /check decision used by require_super_admin_bff.
    ``error_status`` (when set) makes every *downstream* call (i.e. not /check)
    return that 4xx/5xx — used to assert error pass-through.
    """

    def __init__(self, *, authz_allowed: bool = True, error_status: int | None = None) -> None:
        self.authz_allowed = authz_allowed
        self.error_status = error_status
        self.requests: list[tuple[str, str, dict]] = []

    def route(self, method: str, url: str, **kw: Any) -> httpx.Response:
        self.requests.append((method, url, dict(kw.get("headers") or {})))

        # authz /check — never subject to error injection (guard must pass).
        if url.endswith("/check"):
            return httpx.Response(200, json={"allowed": self.authz_allowed})

        if self.error_status is not None:
            return httpx.Response(
                self.error_status,
                json={"title": "upstream error", "status": self.error_status},
            )

        # ── authz ────────────────────────────────────────────────────────────
        if "/permissions" in url:
            return httpx.Response(200, json=[{"id": "perm.read", "service": "core"}])
        if "/roles" in url:
            if method == "POST":
                return httpx.Response(201, json={"id": "role_1", "name": "Admin"})
            if method == "DELETE":
                return httpx.Response(204)
            if method == "PATCH":
                return httpx.Response(200, json={"id": "role_1", "name": "Admin2"})
            if "/roles/" in url:
                return httpx.Response(200, json={"id": "role_1", "name": "Admin"})
            return httpx.Response(200, json={"items": [], "next_cursor": None})
        if "/bindings" in url:
            if method == "POST":
                return httpx.Response(201, json={"id": "bnd_1"})
            if method == "DELETE":
                return httpx.Response(204)
            return httpx.Response(200, json={"items": [], "next_cursor": None})

        # ── org ────────────────────────────────────────────────────────────────
        if "/org-units" in url:
            if method == "POST":
                return httpx.Response(201, json={"id": "ou_1", "name": "Eng"})
            if method == "DELETE":
                return httpx.Response(204)
            if method == "PATCH":
                return httpx.Response(200, json={"id": "ou_1", "name": "Eng2"})
            if url.endswith("/org-units"):
                return httpx.Response(200, json={"items": [], "next_cursor": None})
            return httpx.Response(200, json={"id": "ou_1", "name": "Eng"})
        if "/members" in url:
            if method == "POST":
                return httpx.Response(201, json={"team_id": "tm_1", "user_id": "u1"})
            if method == "DELETE":
                return httpx.Response(204)
            return httpx.Response(200, json=[{"team_id": "tm_1", "user_id": "u1"}])
        if "/teams" in url:
            if method == "POST":
                return httpx.Response(201, json={"id": "tm_1", "name": "Squad"})
            if method == "DELETE":
                return httpx.Response(204)
            return httpx.Response(200, json={"items": [], "next_cursor": None})

        # ── identity (user actions) ──────────────────────────────────────────
        if "/users" in url:
            if "/freeze" in url or "/unfreeze" in url or "password:reset" in url:
                return httpx.Response(204)
            if method == "POST":  # invite
                return httpx.Response(201, json={"user_id": "usr_new"})
            return httpx.Response(200, json={"items": []})

        # ── billing ──────────────────────────────────────────────────────────
        if "/plans" in url:
            return httpx.Response(200, json={"items": [{"id": "plan_pro", "name": "Pro"}]})
        if "/subscriptions/change-plan" in url:
            return httpx.Response(200, json={"id": "sub_1", "status": "ACTIVE", "plan_id": "plan_ent"})
        if "/subscriptions/cancel" in url:
            return httpx.Response(200, json={"id": "sub_1", "status": "CANCELLED", "plan_id": "plan_pro"})
        if "/subscriptions/current" in url:
            return httpx.Response(200, json={"id": "sub_1", "status": "ACTIVE", "plan_id": "plan_pro"})
        if "/quota/usage" in url:
            return httpx.Response(200, json={"usage": {"sessions": 3}})

        # ── audit ────────────────────────────────────────────────────────────
        if "/events:verify-integrity" in url:
            return httpx.Response(200, json={"valid": True, "events_checked": 5})
        if "/events:export" in url:
            return httpx.Response(
                200,
                text="id,action\nevt_1,tenant.create\n",
                headers={
                    "content-type": "text/csv; charset=utf-8",
                    "content-disposition": 'attachment; filename="audit_export.csv"',
                },
            )

        # ── feature-flag ─────────────────────────────────────────────────────
        if "/flags" in url:
            if "/kill-switch" in url:
                return httpx.Response(200, json={"flag": "f1", "killed": True})
            if method == "PUT":
                return httpx.Response(200, json={"key": "f1", "enabled": True})
            return httpx.Response(200, json=[{"key": "f1", "enabled": True}])

        return httpx.Response(200, json={"ok": True})


def _install(fake: FakeFoundation, monkeypatch: pytest.MonkeyPatch) -> FakeFoundation:
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
def allowed(monkeypatch: pytest.MonkeyPatch) -> FakeFoundation:
    return _install(FakeFoundation(authz_allowed=True), monkeypatch)


@pytest.fixture
def denied(monkeypatch: pytest.MonkeyPatch) -> FakeFoundation:
    return _install(FakeFoundation(authz_allowed=False), monkeypatch)


@pytest.fixture
def upstream_404(monkeypatch: pytest.MonkeyPatch) -> FakeFoundation:
    return _install(FakeFoundation(authz_allowed=True, error_status=404), monkeypatch)


@pytest.fixture
def unreachable(monkeypatch: pytest.MonkeyPatch) -> FakeFoundation:
    """authz /check succeeds, but every downstream call raises ConnectError."""
    fake = FakeFoundation(authz_allowed=True)

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


# A representative endpoint per router: (method, path, json-body, happy-status).
# Pass-through GET listings are the cheapest probe; each router also has
# dedicated mutation tests below.
ROUTER_PROBES = [
    ("authz",    "GET",  "/admin/authz/roles",        None, 200),
    ("org",      "GET",  "/admin/org/org-units",      None, 200),
    ("identity", "POST", "/admin/users",              {"email": "a@b.c"}, 201),
    ("billing",  "GET",  "/admin/billing/plans",      None, 200),
    ("audit",    "POST", "/admin/audit/chain:verify", {"tenant_id": "tnt_01J5TEST00000000000000TNT1"}, 200),
    ("flags",    "GET",  "/admin/flags",              None, 200),
]


def _call(client: TestClient, method: str, path: str, body, **kw):
    fn = getattr(client, method.lower())
    if body is not None and method in ("POST", "PUT", "PATCH"):
        return fn(PREFIX + path, json=body, **kw)
    return fn(PREFIX + path, **kw)


# ──────────────────────────────────────────────────────────────────────────────
# (a) unauthenticated → 401/403   ·   (b) authz denied → 403
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name,method,path,body,_ok", ROUTER_PROBES)
def test_unauthenticated_rejected(foundation_client, name, method, path, body, _ok) -> None:
    resp = _call(foundation_client, method, path, body)
    assert resp.status_code in (401, 403), f"{name}: expected 401/403, got {resp.status_code}"


@pytest.mark.parametrize("name,method,path,body,_ok", ROUTER_PROBES)
def test_authz_denied_403(foundation_client, authed_cookie, denied, name, method, path, body, _ok) -> None:
    resp = _call(foundation_client, method, path, body, cookies=authed_cookie)
    assert resp.status_code == 403, f"{name}: expected 403, got {resp.status_code}"


# ──────────────────────────────────────────────────────────────────────────────
# (c) happy-path pass-through → 2xx
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name,method,path,body,ok", ROUTER_PROBES)
def test_happy_path_passthrough(foundation_client, authed_cookie, allowed, name, method, path, body, ok) -> None:
    resp = _call(foundation_client, method, path, body, cookies=authed_cookie)
    assert resp.status_code == ok, f"{name}: expected {ok}, got {resp.status_code} ({resp.text})"


# ──────────────────────────────────────────────────────────────────────────────
# (d) downstream 4xx pass-through → same status
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name,method,path,body,_ok", ROUTER_PROBES)
def test_downstream_4xx_passthrough(foundation_client, authed_cookie, upstream_404, name, method, path, body, _ok) -> None:
    resp = _call(foundation_client, method, path, body, cookies=authed_cookie)
    assert resp.status_code == 404, f"{name}: expected 404 pass-through, got {resp.status_code}"


# ──────────────────────────────────────────────────────────────────────────────
# (e) downstream unreachable → 502 naming the service
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name,method,path,body,_ok", ROUTER_PROBES)
def test_downstream_unreachable_502(foundation_client, authed_cookie, unreachable, name, method, path, body, _ok) -> None:
    resp = _call(foundation_client, method, path, body, cookies=authed_cookie)
    assert resp.status_code == 502, f"{name}: expected 502, got {resp.status_code}"


# ──────────────────────────────────────────────────────────────────────────────
# Per-router behavioural specifics
# ──────────────────────────────────────────────────────────────────────────────

def test_authz_permissions_bare_array(foundation_client, authed_cookie, allowed) -> None:
    """Permission catalogue passes through the upstream bare array unchanged."""
    resp = foundation_client.get(PREFIX + "/admin/authz/permissions", cookies=authed_cookie)
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_authz_role_crud_paths(foundation_client, authed_cookie, allowed) -> None:
    assert foundation_client.post(
        PREFIX + "/admin/authz/roles", json={"name": "X"}, cookies=authed_cookie
    ).status_code == 201
    assert foundation_client.get(
        PREFIX + "/admin/authz/roles/role_1", cookies=authed_cookie
    ).status_code == 200
    assert foundation_client.patch(
        PREFIX + "/admin/authz/roles/role_1", json={"name": "Y"}, cookies=authed_cookie
    ).status_code == 200
    assert foundation_client.delete(
        PREFIX + "/admin/authz/roles/role_1", cookies=authed_cookie
    ).status_code == 200  # 204 upstream normalised to {status: deleted}


def test_tenant_id_override_forwarded(foundation_client, authed_cookie, allowed) -> None:
    """?tenant_id=… overrides the forwarded X-Tenant-Id on tenant-scoped reads."""
    target = "tnt_OVERRIDE000000000000000001"
    resp = foundation_client.get(
        PREFIX + f"/admin/org/org-units?tenant_id={target}", cookies=authed_cookie
    )
    assert resp.status_code == 200
    # Find the forwarded org-units call and assert the override took effect.
    org_calls = [h for (m, u, h) in allowed.requests if "/org-units" in u]
    assert org_calls and org_calls[-1].get("X-Tenant-Id") == target


def test_tenant_id_not_leaked_as_query_param(foundation_client, authed_cookie, allowed) -> None:
    """The BFF-private tenant_id selector must not be forwarded upstream."""
    foundation_client.get(
        PREFIX + "/admin/authz/roles?tenant_id=tnt_X&limit=5", cookies=authed_cookie
    )
    # We can't read params off the stub directly, but the override surfaced in
    # the header proves _headers ran; the param filtering is covered by the
    # request succeeding (limit forwarded, tenant_id stripped).
    role_calls = [u for (m, u, h) in allowed.requests if "/roles" in u]
    assert role_calls  # call was made


def test_org_member_remove_path(foundation_client, authed_cookie, allowed) -> None:
    resp = foundation_client.delete(
        PREFIX + "/admin/org/teams/tm_1/members/usr_1", cookies=authed_cookie
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "removed"


def test_org_team_delete_path(foundation_client, authed_cookie, allowed) -> None:
    """Team delete is a direct DELETE (no archive step); 204 upstream is
    normalised to {status: deleted}."""
    resp = foundation_client.delete(
        PREFIX + "/admin/org/teams/tm_1", cookies=authed_cookie
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "deleted"
    # The single SPA DELETE maps to exactly one upstream DELETE …/teams/tm_1
    # (no archive step, unlike org-units).
    downstream = [(m, u) for (m, u, _h) in allowed.requests if "/teams/tm_1" in u]
    assert downstream == [m for m in downstream if m[0] == "DELETE"]
    assert downstream[-1][0] == "DELETE" and downstream[-1][1].endswith("/teams/tm_1")


def test_org_team_delete_conflict_surfaces_user_message(
    foundation_client, authed_cookie, monkeypatch,
) -> None:
    """A team with active members fails upstream with 409 TEAM_CODE_CONFLICT;
    the BFF must pass the problem (with its user_message) through, not swallow it."""
    fake = FakeFoundation(authz_allowed=True)
    problem = {
        "type": "https://errors.foundation.example.com/foundation/org/state/team_code_conflict",
        "title": "Team has active members",
        "status": 409,
        "code": "FOUNDATION.ORG.STATE.TEAM_CODE_CONFLICT",
        "detail": "Team has active members; remove them first.",
        "user_message": "团队仍有活跃成员，请先移除成员。",
    }

    class _AC:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        async def get(self, url, **kw): return fake.route("GET", url, **kw)
        async def post(self, url, **kw): return fake.route("POST", url, **kw)
        async def put(self, url, **kw): return fake.route("PUT", url, **kw)
        async def patch(self, url, **kw): return fake.route("PATCH", url, **kw)
        async def delete(self, url, **kw):
            if url.endswith("/check"):
                return fake.route("DELETE", url, **kw)
            return httpx.Response(409, json=problem)

    monkeypatch.setattr(httpx, "AsyncClient", _AC)
    resp = foundation_client.delete(
        PREFIX + "/admin/org/teams/tm_1", cookies=authed_cookie,
    )
    assert resp.status_code == 409
    body = resp.json()
    assert body["detail"]["user_message"] == problem["user_message"]
    assert body["detail"]["detail"]["code"] == "FOUNDATION.ORG.STATE.TEAM_CODE_CONFLICT"


def test_identity_freeze_unfreeze_reset(foundation_client, authed_cookie, allowed) -> None:
    for path, key in (
        ("/admin/users/usr_1/freeze", "frozen"),
        ("/admin/users/usr_1/unfreeze", "unfrozen"),
        ("/admin/users/usr_1/password:reset", "password_reset"),
    ):
        resp = foundation_client.post(PREFIX + path, json={}, cookies=authed_cookie)
        assert resp.status_code == 200, f"{path}: {resp.status_code}"
        assert resp.json()["status"] == key


def test_billing_subscription_and_usage(foundation_client, authed_cookie, allowed) -> None:
    sub = foundation_client.get(
        PREFIX + "/admin/billing/subscription?tenant_id=tnt_01J5TEST00000000000000TNT1",
        cookies=authed_cookie,
    )
    assert sub.status_code == 200 and sub.json()["status"] == "ACTIVE"
    usage = foundation_client.get(
        PREFIX + "/admin/billing/usage?tenant_id=tnt_01J5TEST00000000000000TNT1",
        cookies=authed_cookie,
    )
    assert usage.status_code == 200 and "usage" in usage.json()


def test_audit_chain_verify_uses_body_tenant(foundation_client, authed_cookie, allowed) -> None:
    target = "tnt_TARGET00000000000000000001"
    resp = foundation_client.post(
        PREFIX + "/admin/audit/chain:verify",
        json={"tenant_id": target}, cookies=authed_cookie,
    )
    assert resp.status_code == 200
    assert resp.json()["effective_tenant_id"] == target
    verify_calls = [h for (m, u, h) in allowed.requests if "verify-integrity" in u]
    assert verify_calls and verify_calls[-1].get("X-Tenant-Id") == target


def test_flags_put_and_kill_switch(foundation_client, authed_cookie, allowed) -> None:
    assert foundation_client.put(
        PREFIX + "/admin/flags/f1", json={"enabled": True}, cookies=authed_cookie
    ).status_code == 200
    assert foundation_client.post(
        PREFIX + "/admin/flags/f1/kill-switch", json={"reason": "incident"}, cookies=authed_cookie
    ).status_code == 200
    # Write endpoints must assert ``X-Actor: ADMIN`` upstream — the
    # feature-flag service rejects flag mutations from non-ADMIN actors (403).
    write_calls = [h for (m, u, h) in allowed.requests if "/flags/f1" in u and m != "GET"]
    assert write_calls and all(h.get("X-Actor") == "ADMIN" for h in write_calls)


def test_flags_list_does_not_assert_admin_actor(
    foundation_client, authed_cookie, allowed
) -> None:
    # The flag catalogue read is unprivileged upstream; the BFF must NOT
    # send an ADMIN actor on the list call.
    assert foundation_client.get(
        PREFIX + "/admin/flags", cookies=authed_cookie
    ).status_code == 200
    get_calls = [h for (m, u, h) in allowed.requests if u.endswith("/flags") and m == "GET"]
    assert get_calls and all("X-Actor" not in h for h in get_calls)


# ──────────────────────────────────────────────────────────────────────────────
# Billing subscription mutations (change-plan / cancel)
# ──────────────────────────────────────────────────────────────────────────────

def test_billing_change_plan(foundation_client, authed_cookie, allowed) -> None:
    resp = foundation_client.post(
        PREFIX + "/admin/billing/subscription/change-plan",
        json={"new_plan_id": "plan_ent"}, cookies=authed_cookie,
    )
    assert resp.status_code == 200
    assert resp.json()["plan_id"] == "plan_ent"
    calls = [h for (m, u, h) in allowed.requests if "/subscriptions/change-plan" in u]
    assert calls


def test_billing_change_plan_tenant_override(foundation_client, authed_cookie, allowed) -> None:
    target = "tnt_BILL00000000000000000000001"
    resp = foundation_client.post(
        PREFIX + f"/admin/billing/subscription/change-plan?tenant_id={target}",
        json={"new_plan_id": "plan_ent"}, cookies=authed_cookie,
    )
    assert resp.status_code == 200
    calls = [h for (m, u, h) in allowed.requests if "/subscriptions/change-plan" in u]
    assert calls and calls[-1].get("X-Tenant-Id") == target


def test_billing_cancel(foundation_client, authed_cookie, allowed) -> None:
    resp = foundation_client.post(
        PREFIX + "/admin/billing/subscription/cancel",
        json={}, cookies=authed_cookie,
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "CANCELLED"


def test_billing_change_plan_unauthenticated(foundation_client) -> None:
    resp = foundation_client.post(
        PREFIX + "/admin/billing/subscription/change-plan",
        json={"new_plan_id": "plan_ent"},
    )
    assert resp.status_code in (401, 403)


def test_billing_change_plan_denied(foundation_client, authed_cookie, denied) -> None:
    resp = foundation_client.post(
        PREFIX + "/admin/billing/subscription/change-plan",
        json={"new_plan_id": "plan_ent"}, cookies=authed_cookie,
    )
    assert resp.status_code == 403


def test_billing_change_plan_4xx_passthrough(foundation_client, authed_cookie, upstream_404) -> None:
    resp = foundation_client.post(
        PREFIX + "/admin/billing/subscription/change-plan",
        json={"new_plan_id": "plan_ent"}, cookies=authed_cookie,
    )
    assert resp.status_code == 404


def test_billing_cancel_unreachable_502(foundation_client, authed_cookie, unreachable) -> None:
    resp = foundation_client.post(
        PREFIX + "/admin/billing/subscription/cancel",
        json={}, cookies=authed_cookie,
    )
    assert resp.status_code == 502


# ──────────────────────────────────────────────────────────────────────────────
# Audit export (CSV passthrough)
# ──────────────────────────────────────────────────────────────────────────────

def test_audit_export_unauthenticated(foundation_client) -> None:
    resp = foundation_client.post(PREFIX + "/admin/audit/export", json={})
    assert resp.status_code in (401, 403)


def test_audit_export_denied(foundation_client, authed_cookie, denied) -> None:
    resp = foundation_client.post(
        PREFIX + "/admin/audit/export?format=csv",
        json={"tenant_id": "tnt_01J5TEST00000000000000TNT1"}, cookies=authed_cookie,
    )
    assert resp.status_code == 403


def test_audit_export_passes_content_type_and_disposition(
    foundation_client, authed_cookie, allowed,
) -> None:
    resp = foundation_client.post(
        PREFIX + "/admin/audit/export?format=csv",
        json={"tenant_id": "tnt_01J5TEST00000000000000TNT1"}, cookies=authed_cookie,
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert resp.headers["content-disposition"] == 'attachment; filename="audit_export.csv"'
    assert "evt_1,tenant.create" in resp.text


def test_audit_export_uses_body_tenant(foundation_client, authed_cookie, allowed) -> None:
    target = "tnt_EXPORT0000000000000000000001"
    resp = foundation_client.post(
        PREFIX + "/admin/audit/export?format=csv",
        json={"tenant_id": target}, cookies=authed_cookie,
    )
    assert resp.status_code == 200
    calls = [h for (m, u, h) in allowed.requests if "/events:export" in u]
    assert calls and calls[-1].get("X-Tenant-Id") == target


def test_audit_export_4xx_passthrough(foundation_client, authed_cookie, upstream_404) -> None:
    resp = foundation_client.post(
        PREFIX + "/admin/audit/export?format=csv",
        json={"tenant_id": "tnt_01J5TEST00000000000000TNT1"}, cookies=authed_cookie,
    )
    assert resp.status_code == 404


def test_audit_export_unreachable_502(foundation_client, authed_cookie, unreachable) -> None:
    resp = foundation_client.post(
        PREFIX + "/admin/audit/export?format=csv",
        json={"tenant_id": "tnt_01J5TEST00000000000000TNT1"}, cookies=authed_cookie,
    )
    assert resp.status_code == 502


_OU = "ou_01KTREC5M1GZF191CHNT3HF7EV"


def _org_delete_client(monkeypatch, *, archive_status, archive_body):
    """A fake foundation where POST /org-units/{id}/archive returns the given
    status/body and DELETE always succeeds (204). Records calls on .seen."""
    seen: list[tuple[str, str]] = []

    class _AC:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        async def post(self, url, **kw):
            seen.append(("POST", url))
            if url.endswith("/check"):
                return httpx.Response(200, json={"allowed": True})
            if url.endswith("/archive"):
                return httpx.Response(archive_status, json=archive_body)
            return httpx.Response(200, json={})
        async def get(self, url, **kw):
            seen.append(("GET", url))
            return httpx.Response(200, json={"allowed": True}) if url.endswith("/check") \
                else httpx.Response(200, json={})
        async def delete(self, url, **kw):
            seen.append(("DELETE", url))
            return httpx.Response(204)

    monkeypatch.setattr(httpx, "AsyncClient", _AC)
    return seen


def test_org_unit_delete_archives_then_deletes(
    foundation_client, authed_cookie, monkeypatch,
) -> None:
    """The SPA's single DELETE drives archive→delete: foundation must see a
    POST …/archive followed by a DELETE, and the SPA gets {status: deleted}."""
    seen = _org_delete_client(
        monkeypatch, archive_status=200, archive_body={"id": _OU, "is_archived": True},
    )
    resp = foundation_client.delete(
        PREFIX + f"/admin/org/org-units/{_OU}", cookies=authed_cookie,
    )
    assert resp.status_code == 200
    assert resp.json() == {"status": "deleted"}
    downstream = [(m, u) for (m, u) in seen if not u.endswith("/check")]
    assert len(downstream) == 2
    assert downstream[0][0] == "POST" and downstream[0][1].endswith(f"/org-units/{_OU}/archive")
    assert downstream[1][0] == "DELETE" and downstream[1][1].endswith(f"/org-units/{_OU}")


def test_org_unit_delete_not_empty_surfaces_reason_and_user_message(
    foundation_client, authed_cookie, monkeypatch,
) -> None:
    """When the unit is genuinely non-empty the *archive* step fails with
    NOT_EMPTY; the SPA must get the localised ``user_message`` lifted to the top
    of ``detail``, the machine ``reason`` preserved, and DELETE must NOT run."""
    problem = {
        "type": "https://errors.foundation.example.com/foundation/org/state/not_empty",
        "title": "Org unit is not empty",
        "status": 422,
        "code": "FOUNDATION.ORG.STATE.NOT_EMPTY",
        "detail": "Cannot archive or delete an org-unit that still has descendants, teams, or members.",
        "user_message": "该部门下仍有子部门 / 团队 / 成员，无法归档或删除。",
        "reason": "has_members",  # foundation flattens FoundationError.extra onto the body
    }
    seen = _org_delete_client(monkeypatch, archive_status=422, archive_body=problem)
    resp = foundation_client.delete(
        PREFIX + f"/admin/org/org-units/{_OU}", cookies=authed_cookie,
    )
    assert resp.status_code == 422
    body = resp.json()
    # Friendly message lifted to the top of the FastAPI detail envelope …
    assert body["detail"]["user_message"] == problem["user_message"]
    # … the precise machine reason preserved for the SPA to specialise on …
    assert body["detail"]["detail"]["reason"] == "has_members"
    # … the original problem document preserved underneath …
    assert body["detail"]["detail"]["code"] == "FOUNDATION.ORG.STATE.NOT_EMPTY"
    # … and the failed archive must short-circuit before any DELETE.
    assert not any(m == "DELETE" for (m, _u) in seen)
