"""Tests: GET /billing/me and GET /auth/mfa/methods.

Covers:
1. /billing/me — unauthenticated → 401/403
2. /billing/me — active subscription → composed plan + usage returned
3. /billing/me — inactive subscription (status != ACTIVE) → plan: null + usage
4. /billing/me — no plan_id in subscription → plan: null + usage
5. /billing/me — plan_id not in catalogue → plan: null + usage
6. /auth/mfa/methods — unauthenticated → 401/403
7. /auth/mfa/methods — authenticated, identity returns list → list forwarded
8. /auth/mfa/methods — authenticated, empty list → [] forwarded
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

# ──────────────────────────────────────────────────────────────────────────────
# Fake upstream that handles billing + identity /mfa/methods
# ──────────────────────────────────────────────────────────────────────────────

class FakeBillingFoundation:
    """Configurable stub for billing service + identity MFA methods."""

    def __init__(
        self,
        *,
        sub_status: str = "ACTIVE",
        plan_id: str | None = "plan_pro",
        plans: list[dict] | None = None,
        usage: dict | None = None,
        mfa_methods: list[dict] | None = None,
    ) -> None:
        self.sub_status = sub_status
        self.plan_id = plan_id
        self.plans: list[dict] = plans if plans is not None else [
            {
                "id": "plan_pro",
                "name": "Pro",
                "tier": "PRO",
                "quotas": {"sessions_per_month": 1000, "storage_gb": 50},
            }
        ]
        self.usage: dict = usage if usage is not None else {"sessions_per_month": 42}
        self.mfa_methods: list[dict] = mfa_methods if mfa_methods is not None else [
            {
                "id": "mfa_01",
                "kind": "TOTP",
                "label": "Authenticator",
                "enrolled_at": "2026-01-01T00:00:00Z",
                "last_used_at": None,
            }
        ]
        self.calls: list[tuple[str, str]] = []

    def route(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        self.calls.append((method, url))

        # identity /sessions/me — needed for require_auth
        if "/sessions/me" in url:
            return httpx.Response(200, json={
                "user_id": "usr_01J5TEST0000000000000USER1",
                "tenant_id": "tnt_01J5TEST00000000000000TNT1",
                "email": "test@example.com",
                "display_name": "Test User",
                "roles": [],
                "mfa_enabled": False,
                "locale": "zh-CN",
            })

        # identity /mfa/methods
        if "/mfa/methods" in url:
            return httpx.Response(200, json=self.mfa_methods)

        # billing /subscriptions/current
        if "/subscriptions/current" in url:
            body: dict = {"status": self.sub_status}
            if self.plan_id:
                body["plan_id"] = self.plan_id
            return httpx.Response(200, json=body)

        # billing /plans
        if "/plans" in url:
            return httpx.Response(200, json={"items": self.plans})

        # billing /quota/usage
        if "/quota/usage" in url:
            return httpx.Response(200, json={"usage": self.usage})

        return httpx.Response(200, json={"ok": True})


def _make_fake_client(fake: FakeBillingFoundation, monkeypatch: pytest.MonkeyPatch) -> None:
    class _AC:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        async def get(self, url, **kw): return fake.route("GET", url, **kw)
        async def post(self, url, **kw): return fake.route("POST", url, **kw)
        async def delete(self, url, **kw): return fake.route("DELETE", url, **kw)
        async def patch(self, url, **kw): return fake.route("PATCH", url, **kw)

    monkeypatch.setattr(httpx, "AsyncClient", _AC)


# ──────────────────────────────────────────────────────────────────────────────
# /billing/me
# ──────────────────────────────────────────────────────────────────────────────

def test_billing_me_unauthenticated(client) -> None:
    """No session cookie → 401 or 403."""
    resp = client.get("/api/bff/v1/billing/me")
    assert resp.status_code in (401, 403)


def test_billing_me_active_subscription_returns_plan(
    client,
    authed_cookie: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Active subscription with matching plan → full QuotaInfo returned."""
    fake = FakeBillingFoundation()
    _make_fake_client(fake, monkeypatch)

    resp = client.get("/api/bff/v1/billing/me", cookies=authed_cookie)
    assert resp.status_code == 200, resp.text

    body = resp.json()
    assert body["tenant_id"] == "tnt_01J5TEST00000000000000TNT1"
    assert body["plan"] is not None
    assert body["plan"]["id"] == "plan_pro"
    assert body["plan"]["name"] == "Pro"
    assert body["plan"]["tier"] == "PRO"
    assert body["plan"]["limits"] == {"sessions_per_month": 1000, "storage_gb": 50}
    assert body["usage"] == {"sessions_per_month": 42}


def test_billing_me_inactive_subscription_returns_null_plan(
    client,
    authed_cookie: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Subscription status != ACTIVE → plan is null, usage still present."""
    fake = FakeBillingFoundation(sub_status="EXPIRED")
    _make_fake_client(fake, monkeypatch)

    resp = client.get("/api/bff/v1/billing/me", cookies=authed_cookie)
    assert resp.status_code == 200, resp.text

    body = resp.json()
    assert body["plan"] is None
    assert body["usage"] == {"sessions_per_month": 42}


def test_billing_me_no_plan_id_returns_null_plan(
    client,
    authed_cookie: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Active subscription but no plan_id → plan is null."""
    fake = FakeBillingFoundation(plan_id=None)
    _make_fake_client(fake, monkeypatch)

    resp = client.get("/api/bff/v1/billing/me", cookies=authed_cookie)
    assert resp.status_code == 200, resp.text

    body = resp.json()
    assert body["plan"] is None


def test_billing_me_plan_not_in_catalogue_returns_null_plan(
    client,
    authed_cookie: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """plan_id in subscription doesn't match any catalogue entry → null."""
    fake = FakeBillingFoundation(
        plan_id="plan_unknown",
        plans=[{"id": "plan_pro", "name": "Pro", "tier": "PRO", "quotas": {}}],
    )
    _make_fake_client(fake, monkeypatch)

    resp = client.get("/api/bff/v1/billing/me", cookies=authed_cookie)
    assert resp.status_code == 200, resp.text

    body = resp.json()
    assert body["plan"] is None


def test_billing_me_tenant_id_from_cookie_not_query(
    client,
    authed_cookie: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """tenant_id in response must match the cookie, not any external param."""
    fake = FakeBillingFoundation()
    _make_fake_client(fake, monkeypatch)

    # Even if a malicious caller tries to inject a different tenant via query,
    # the response should reflect the cookie's tenant_id.
    resp = client.get(
        "/api/bff/v1/billing/me?tenant_id=evil_tenant",
        cookies=authed_cookie,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["tenant_id"] == "tnt_01J5TEST00000000000000TNT1"


# ──────────────────────────────────────────────────────────────────────────────
# /auth/mfa/methods
# ──────────────────────────────────────────────────────────────────────────────

def test_mfa_methods_unauthenticated(client) -> None:
    """No session cookie → 401 or 403."""
    resp = client.get("/api/bff/v1/auth/mfa/methods")
    assert resp.status_code in (401, 403)


def test_mfa_methods_returns_list(
    client,
    authed_cookie: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Authenticated → list of MFA methods forwarded from identity."""
    fake = FakeBillingFoundation()
    _make_fake_client(fake, monkeypatch)

    resp = client.get("/api/bff/v1/auth/mfa/methods", cookies=authed_cookie)
    assert resp.status_code == 200, resp.text

    body = resp.json()
    assert isinstance(body, list)
    assert len(body) == 1
    assert body[0]["id"] == "mfa_01"
    assert body[0]["kind"] == "TOTP"


def test_mfa_methods_empty_list(
    client,
    authed_cookie: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty MFA methods list forwarded as-is (no 2FA enrolled)."""
    fake = FakeBillingFoundation(mfa_methods=[])
    _make_fake_client(fake, monkeypatch)

    resp = client.get("/api/bff/v1/auth/mfa/methods", cookies=authed_cookie)
    assert resp.status_code == 200, resp.text

    body = resp.json()
    assert body == []
