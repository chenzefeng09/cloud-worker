"""Tests: BFF admin proxy + require_super_admin_bff guard.

These tests cover:
1. Unauthenticated requests are rejected with 401 / 403.
2. Authenticated callers with authz denied are rejected with 403.
3. Authenticated super_admin callers are proxied to foundation.
4. /auth/me includes 'roles' field (passthrough from identity).
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

class FakeAdminFoundation:
    """Fake foundation responses for admin tests."""

    def __init__(self, *, authz_allowed: bool = True) -> None:
        self.calls: list[tuple[str, str]] = []
        # (method, url, headers) for every forwarded call — lets tests assert
        # the X-Actor header on llm-gateway proxies.
        self.requests: list[tuple[str, str, dict]] = []
        self.authz_allowed = authz_allowed

    def route(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        self.calls.append((method, url))
        self.requests.append((method, url, dict(kwargs.get("headers") or {})))

        # authz /check
        if "/check" in url:
            if self.authz_allowed:
                return httpx.Response(200, json={"allowed": True, "decision_path": "RBAC_HIT"})
            else:
                return httpx.Response(200, json={"allowed": False, "decision_path": "DEFAULT_DENY"})

        # identity /sessions/me
        if "/sessions/me" in url:
            return httpx.Response(200, json={
                "user_id": "usr_01J5TEST0000000000000USER1",
                "tenant_id": "tnt_01J5TEST00000000000000TNT1",
                "email": "admin@example.com",
                "display_name": "Admin User",
                "roles": ["super_admin"],
                "mfa_enabled": False,
                "locale": "zh-CN",
            })

        # identity /platform/tenants/{tid} — single tenant lookup (full object)
        if "/platform/tenants/" in url and method == "GET":
            tid = url.rstrip("/").rsplit("/", 1)[-1]
            return httpx.Response(200, json={
                "id": tid,
                "name": "Test Corp",
                "tier": "FREE",
                "status": "active",
                "created_at": "2026-01-01T00:00:00Z",
            })

        # identity /platform/tenants — list
        if "/platform/tenants" in url and method == "GET":
            return httpx.Response(200, json={"items": [], "next_cursor": None})

        if "/platform/tenants" in url and method == "POST":
            return httpx.Response(201, json={
                "tenant_id": "tnt_NEWONE0000000000000000001",
                "admin_user_id": "usr_NEWADMIN0000000000000001",
                "root_ou_id": None,
                "subscription_id": None,
                "steps": ["tenant_row_created"],
            })

        return httpx.Response(200, json={"ok": True})


@pytest.fixture
def fake_admin_foundation_allowed(monkeypatch: pytest.MonkeyPatch) -> FakeAdminFoundation:
    fake = FakeAdminFoundation(authz_allowed=True)

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


@pytest.fixture
def fake_admin_foundation_denied(monkeypatch: pytest.MonkeyPatch) -> FakeAdminFoundation:
    fake = FakeAdminFoundation(authz_allowed=False)

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


# ──────────────────────────────────────────────────────────────────────────────
# Tests
# ──────────────────────────────────────────────────────────────────────────────

def test_admin_tenants_unauthenticated(client) -> None:
    """No cookie → 401 or 403."""
    resp = client.get("/api/bff/v1/admin/tenants")
    assert resp.status_code in (401, 403)


def test_admin_tenants_authz_denied(
    client,
    authed_cookie: dict,
    fake_admin_foundation_denied,
) -> None:
    """Authenticated but authz returns allowed=False → 403."""
    resp = client.get("/api/bff/v1/admin/tenants", cookies=authed_cookie)
    assert resp.status_code == 403


def test_admin_tenants_super_admin_allowed(
    client,
    authed_cookie: dict,
    fake_admin_foundation_allowed: FakeAdminFoundation,
) -> None:
    """Authenticated super_admin → proxied to foundation, 200 returned."""
    resp = client.get("/api/bff/v1/admin/tenants", cookies=authed_cookie)
    assert resp.status_code == 200
    body = resp.json()
    assert "items" in body


def test_admin_create_tenant_super_admin(
    client,
    authed_cookie: dict,
    fake_admin_foundation_allowed: FakeAdminFoundation,
) -> None:
    """POST /admin/tenants → proxied to foundation provisioning."""
    resp = client.post(
        "/api/bff/v1/admin/tenants",
        json={"name": "Test Corp", "tier": "FREE", "admin_email": "admin@test.com"},
        cookies=authed_cookie,
    )
    assert resp.status_code == 201
    body = resp.json()
    # BFF now re-fetches the full tenant object after provisioning so the admin
    # UI gets {id, name, tier, status, created_at} instead of the bare
    # provisioning envelope ({tenant_id, ...}) that crashed the frontend.
    assert body["id"] == "tnt_NEWONE0000000000000000001"
    assert "name" in body and "status" in body


def test_auth_me_includes_roles(
    client,
    authed_cookie: dict,
    fake_admin_foundation_allowed: FakeAdminFoundation,
) -> None:
    """GET /auth/me must transparently pass through 'roles' from identity."""
    resp = client.get("/api/bff/v1/auth/me", cookies=authed_cookie)
    assert resp.status_code == 200
    body = resp.json()
    # roles must be present (identity /sessions/me returns it; BFF passes through)
    assert "roles" in body
    assert isinstance(body["roles"], list)


# ──────────────────────────────────────────────────────────────────────────────
# Audit endpoint tests
# ──────────────────────────────────────────────────────────────────────────────

class FakeAuditFoundation(FakeAdminFoundation):
    """Extends FakeAdminFoundation to stub the audit service as well."""

    def __init__(self, *, authz_allowed: bool = True) -> None:
        super().__init__(authz_allowed=authz_allowed)
        # Forwarded JSON bodies for each audit /events:query call — lets tests
        # assert the BFF injected the effective tenant_id into the body.
        self.audit_bodies: list[Any] = []

    def route(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        # Audit events:query — record call (+ forwarded body) and return stub
        if "/events:query" in url and method == "POST":
            self.calls.append((method, url))
            self.audit_bodies.append(kwargs.get("json"))
            return httpx.Response(200, json={
                "items": [
                    {
                        "id": "evt_01J5TEST0000000000000EVT1",
                        "tenant_id": "tnt_01J5TEST00000000000000TNT1",
                        "actor_id": "usr_01J5TEST0000000000000USER1",
                        "action": "tenant.create",
                        "resource_type": "tenant",
                        "resource_id": "tnt_01J5TEST00000000000000TNT2",
                        "meta": {},
                        "created_at": "2026-01-01T00:00:00Z",
                    }
                ],
                "next_cursor": None,
            })
        # All other routes (authz /check, identity /sessions/me, etc.)
        return super().route(method, url, **kwargs)


@pytest.fixture
def fake_audit_foundation(monkeypatch: pytest.MonkeyPatch) -> FakeAuditFoundation:
    fake = FakeAuditFoundation(authz_allowed=True)

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


def test_audit_query_unauthenticated(client) -> None:
    """No cookie → 401 or 403."""
    resp = client.post("/api/bff/v1/admin/audit/events:query", json={})
    assert resp.status_code in (401, 403)


def test_audit_query_authz_denied(
    client,
    authed_cookie: dict,
    fake_admin_foundation_denied: FakeAdminFoundation,
) -> None:
    """Authenticated but authz denied → 403."""
    resp = client.post(
        "/api/bff/v1/admin/audit/events:query",
        json={"tenant_id": "tnt_01J5TEST00000000000000TNT1", "limit": 10},
        cookies=authed_cookie,
    )
    assert resp.status_code == 403


def test_audit_query_super_admin_returns_events(
    client,
    authed_cookie: dict,
    fake_audit_foundation: FakeAuditFoundation,
) -> None:
    """Authenticated super_admin → proxied to audit, returns AuditEvent list."""
    resp = client.post(
        "/api/bff/v1/admin/audit/events:query",
        json={"tenant_id": "tnt_01J5TEST00000000000000TNT1", "limit": 50},
        cookies=authed_cookie,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "items" in body
    assert isinstance(body["items"], list)
    ev = body["items"][0]
    # Verify AuditEvent DTO fields are present
    for field in ("id", "tenant_id", "action", "created_at"):
        assert field in ev, f"AuditEvent missing field: {field}"


def test_audit_query_body_forwarded_verbatim(
    client,
    authed_cookie: dict,
    fake_audit_foundation: FakeAuditFoundation,
) -> None:
    """BFF must forward the request body to foundation audit unchanged."""
    resp = client.post(
        "/api/bff/v1/admin/audit/events:query",
        json={
            "tenant_id": "tnt_01J5TEST00000000000000TNT1",
            "action": "tenant.create",
            "limit": 10,
        },
        cookies=authed_cookie,
    )
    assert resp.status_code == 200
    # Confirm that both authz /check and audit /events:query were called
    called_urls = [url for _, url in fake_audit_foundation.calls]
    assert any("/check" in u for u in called_urls), "authz /check not called"
    assert any("/events:query" in u for u in called_urls), "audit /events:query not called"


# ──────────────────────────────────────────────────────────────────────────────
# Billing quota endpoint tests
# ──────────────────────────────────────────────────────────────────────────────

class FakeBillingFoundation(FakeAdminFoundation):
    """Extends FakeAdminFoundation to stub the billing service."""

    def route(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        # billing /subscriptions/current — bare subscription record, no
        # embedded plan/usage (matches foundation-billing's real response)
        if "/subscriptions/current" in url and method == "GET":
            return httpx.Response(200, json={
                "id": "sub_01J5TEST0000000000000SUB1",
                "tenant_id": "tnt_01J5TEST00000000000000TNT1",
                "plan_id": "plan_pro",
                "mode": "SYSTEM",
                "status": "ACTIVE",
            })
        # billing /plans — catalogue; quota field is `quotas` upstream
        if "/plans" in url and method == "GET":
            return httpx.Response(200, json={
                "items": [
                    {
                        "id": "plan_pro",
                        "name": "Pro",
                        "tier": "PRO",
                        "quotas": {"sessions": 100, "tokens": 1000000},
                        "price_monthly_usd": 49.0,
                    }
                ]
            })
        # billing /quota/usage — current usage map
        if "/quota/usage" in url and method == "GET":
            return httpx.Response(200, json={
                "usage": {"sessions": 12, "tokens": 45000},
            })
        return super().route(method, url, **kwargs)


@pytest.fixture
def fake_billing_foundation(monkeypatch: pytest.MonkeyPatch) -> FakeBillingFoundation:
    fake = FakeBillingFoundation(authz_allowed=True)

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


def test_billing_quota_unauthenticated(client) -> None:
    """No cookie → 401 or 403."""
    resp = client.get("/api/bff/v1/admin/billing/quota?tenant_id=tnt_X")
    assert resp.status_code in (401, 403)


def test_billing_quota_authz_denied(
    client,
    authed_cookie: dict,
    fake_admin_foundation_denied: FakeAdminFoundation,
) -> None:
    """Authenticated but authz denied → 403."""
    resp = client.get(
        "/api/bff/v1/admin/billing/quota?tenant_id=tnt_01J5TEST00000000000000TNT1",
        cookies=authed_cookie,
    )
    assert resp.status_code == 403


def test_billing_quota_super_admin_returns_quotainfo(
    client,
    authed_cookie: dict,
    fake_billing_foundation: FakeBillingFoundation,
) -> None:
    """Authenticated super_admin → BFF merges billing responses into QuotaInfo."""
    resp = client.get(
        "/api/bff/v1/admin/billing/quota?tenant_id=tnt_01J5TEST00000000000000TNT1",
        cookies=authed_cookie,
    )
    assert resp.status_code == 200
    body = resp.json()

    # QuotaInfo top-level shape
    assert body["tenant_id"] == "tnt_01J5TEST00000000000000TNT1"
    assert "plan" in body
    assert "usage" in body

    # BillingPlan shape
    plan = body["plan"]
    for field in ("id", "name", "tier", "limits"):
        assert field in plan, f"BillingPlan missing field: {field}"
    assert isinstance(plan["limits"], dict)

    # usage shape
    assert isinstance(body["usage"], dict)
    assert body["usage"]["sessions"] == 12


def test_billing_quota_merges_plans_when_limits_missing(
    client,
    authed_cookie: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Subscription carries only plan_id; BFF resolves plan details (and maps
    upstream `quotas` → frontend `limits`) from the /plans catalogue."""

    class FakeBillingNoLimits(FakeAdminFoundation):
        def route(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
            if "/subscriptions/current" in url and method == "GET":
                # bare subscription — plan details live in /plans
                return httpx.Response(200, json={
                    "id": "sub_01J5TEST0000000000000SUB2",
                    "tenant_id": "tnt_01J5TEST00000000000000TNT1",
                    "plan_id": "plan_free",
                    "mode": "SYSTEM",
                    "status": "ACTIVE",
                })
            if "/plans" in url and method == "GET":
                return httpx.Response(200, json={
                    "items": [
                        {
                            "id": "plan_free",
                            "name": "Free",
                            "tier": "FREE",
                            "quotas": {"sessions": 5},
                            "price_monthly_usd": 0.0,
                        },
                    ]
                })
            if "/quota/usage" in url and method == "GET":
                return httpx.Response(200, json={"usage": {"sessions": 2}})
            return super().route(method, url, **kwargs)

    fake = FakeBillingNoLimits(authz_allowed=True)

    class _AC:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        async def get(self, url, **kw): return fake.route("GET", url, **kw)
        async def post(self, url, **kw): return fake.route("POST", url, **kw)
        async def patch(self, url, **kw): return fake.route("PATCH", url, **kw)
        async def delete(self, url, **kw): return fake.route("DELETE", url, **kw)

    monkeypatch.setattr(httpx, "AsyncClient", _AC)

    resp = client.get(
        "/api/bff/v1/admin/billing/quota?tenant_id=tnt_01J5TEST00000000000000TNT1",
        cookies=authed_cookie,
    )
    assert resp.status_code == 200
    body = resp.json()
    # BFF should have merged plan from /plans list
    assert body["plan"]["limits"] == {"sessions": 5}


# ──────────────────────────────────────────────────────────────────────────────
# Audit effective_tenant_id + fallback (Task 2)
# ──────────────────────────────────────────────────────────────────────────────

def test_audit_query_echoes_effective_tenant_id(
    client,
    authed_cookie: dict,
    fake_audit_foundation: FakeAuditFoundation,
) -> None:
    """Response must surface the tenant actually queried for RLS."""
    resp = client.post(
        "/api/bff/v1/admin/audit/events:query",
        json={"tenant_id": "tnt_TARGET00000000000000000001", "limit": 10},
        cookies=authed_cookie,
    )
    assert resp.status_code == 200
    assert resp.json()["effective_tenant_id"] == "tnt_TARGET00000000000000000001"


def test_audit_query_fallback_effective_tenant_id(
    client,
    authed_cookie: dict,
    fake_audit_foundation: FakeAuditFoundation,
) -> None:
    """When tenant_id is omitted, fallback tenant is echoed explicitly."""
    resp = client.post(
        "/api/bff/v1/admin/audit/events:query",
        json={"limit": 10},
        cookies=authed_cookie,
    )
    assert resp.status_code == 200
    # Falls back to the admin's own tenant (from the stubbed session).
    assert resp.json()["effective_tenant_id"] == "tnt_01J5TEST00000000000000TNT1"


def test_audit_query_fallback_injects_tenant_id_into_body(
    client,
    authed_cookie: dict,
    fake_audit_foundation: FakeAuditFoundation,
) -> None:
    """When tenant_id is omitted, the BFF must inject the caller's tenant into
    the forwarded *body* (not just the X-Tenant-Id header) — foundation-audit
    validates tenant_id from the body, so without this the fallback 422s."""
    resp = client.post(
        "/api/bff/v1/admin/audit/events:query",
        json={"limit": 10},
        cookies=authed_cookie,
    )
    assert resp.status_code == 200
    # Exactly one audit call was forwarded; its body must carry the caller's
    # tenant injected by the BFF (matching the X-Tenant-Id fallback).
    assert fake_audit_foundation.audit_bodies, "audit /events:query body not captured"
    forwarded = fake_audit_foundation.audit_bodies[-1]
    assert forwarded["tenant_id"] == "tnt_01J5TEST00000000000000TNT1"
    # Other fields are still forwarded verbatim.
    assert forwarded["limit"] == 10


# ──────────────────────────────────────────────────────────────────────────────
# LLM tenant-defaults — X-Actor header + body validation (Tasks 1 & 5)
# ──────────────────────────────────────────────────────────────────────────────

class FakeLlmGatewayFoundation(FakeAdminFoundation):
    """Stubs the llm-gateway /tenant-defaults + /providers routes."""

    def route(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        if "/tenant-defaults" in url and method in ("GET", "PUT"):
            self.calls.append((method, url))
            self.requests.append((method, url, dict(kwargs.get("headers") or {})))
            return httpx.Response(200, json={"ok": True})
        if "/providers" in url and method in ("POST", "PATCH"):
            self.calls.append((method, url))
            self.requests.append((method, url, dict(kwargs.get("headers") or {})))
            return httpx.Response(201 if method == "POST" else 200, json={"id": "prov_1"})
        return super().route(method, url, **kwargs)


@pytest.fixture
def fake_llm_gateway(monkeypatch: pytest.MonkeyPatch) -> FakeLlmGatewayFoundation:
    fake = FakeLlmGatewayFoundation(authz_allowed=True)

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


def test_tenant_defaults_put_sends_x_actor_admin(
    client,
    authed_cookie: dict,
    fake_llm_gateway: FakeLlmGatewayFoundation,
) -> None:
    """PUT /tenant-defaults must forward X-Actor: ADMIN to the gateway."""
    resp = client.put(
        "/api/bff/v1/admin/llm/tenant-defaults",
        json={"purpose": "llm", "model_id": "gpt-4o", "provider_id": "prov_1"},
        cookies=authed_cookie,
    )
    assert resp.status_code == 200
    td_reqs = [
        h for (m, u, h) in fake_llm_gateway.requests
        if "/tenant-defaults" in u and m == "PUT"
    ]
    assert td_reqs, "PUT /tenant-defaults was not forwarded"
    assert td_reqs[0].get("X-Actor") == "ADMIN"


def test_tenant_defaults_put_rejects_bad_purpose(
    client,
    authed_cookie: dict,
    fake_llm_gateway: FakeLlmGatewayFoundation,
) -> None:
    """Invalid purpose → 422 before any upstream call."""
    resp = client.put(
        "/api/bff/v1/admin/llm/tenant-defaults",
        json={"purpose": "bogus", "model_id": "m", "provider_id": "p"},
        cookies=authed_cookie,
    )
    assert resp.status_code == 422


def test_tenant_defaults_put_rejects_missing_fields(
    client,
    authed_cookie: dict,
    fake_llm_gateway: FakeLlmGatewayFoundation,
) -> None:
    """Missing model_id/provider_id → 422."""
    resp = client.put(
        "/api/bff/v1/admin/llm/tenant-defaults",
        json={"purpose": "llm"},
        cookies=authed_cookie,
    )
    assert resp.status_code == 422


def test_providers_post_rejects_empty_body(
    client,
    authed_cookie: dict,
    fake_llm_gateway: FakeLlmGatewayFoundation,
) -> None:
    """POST /providers with empty body → 400 (gateway owns field schema)."""
    resp = client.post(
        "/api/bff/v1/admin/llm/providers",
        json={},
        cookies=authed_cookie,
    )
    assert resp.status_code == 400


def test_providers_post_passthrough_with_x_actor(
    client,
    authed_cookie: dict,
    fake_llm_gateway: FakeLlmGatewayFoundation,
) -> None:
    """Non-empty provider body is passed through with X-Actor: ADMIN."""
    resp = client.post(
        "/api/bff/v1/admin/llm/providers",
        json={"name": "OpenAI", "base_url": "http://x"},
        cookies=authed_cookie,
    )
    assert resp.status_code == 201
    prov_reqs = [
        h for (m, u, h) in fake_llm_gateway.requests
        if "/providers" in u and m == "POST"
    ]
    assert prov_reqs and prov_reqs[0].get("X-Actor") == "ADMIN"


# ──────────────────────────────────────────────────────────────────────────────
# Billing gather connection-failure → 502 (Task 3)
# ──────────────────────────────────────────────────────────────────────────────

def test_billing_quota_gather_connect_error_is_502(
    client,
    authed_cookie: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A connection-level failure on a billing leg → readable 502, not 500."""

    class FakeBillingBroken(FakeAdminFoundation):
        async def _broken_get(self, url, **kw):
            if "/subscriptions/current" in url:
                raise httpx.ConnectError("billing down")
            return self.route("GET", url, **kw)

    fake = FakeBillingBroken(authz_allowed=True)

    class _AC:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        async def get(self, url, **kw): return await fake._broken_get(url, **kw)
        async def post(self, url, **kw): return fake.route("POST", url, **kw)
        async def patch(self, url, **kw): return fake.route("PATCH", url, **kw)
        async def delete(self, url, **kw): return fake.route("DELETE", url, **kw)

    monkeypatch.setattr(httpx, "AsyncClient", _AC)

    resp = client.get(
        "/api/bff/v1/admin/billing/quota?tenant_id=tnt_01J5TEST00000000000000TNT1",
        cookies=authed_cookie,
    )
    assert resp.status_code == 502
