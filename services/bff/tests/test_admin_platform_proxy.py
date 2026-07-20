"""Tests: BFF platform-governance admin proxy (admin_platform.py, modules 7-10).

Covers, for the skills / MCP / knowledge / ops endpoints:
  1. Unauthenticated → 401 / 403.
  2. Authenticated but authz denied → 403.
  3. Authenticated super_admin → upstream proxied, body returned.
  4. Upstream 4xx/5xx → status + body透传 (via _raise_for_upstream).
  5. Upstream unreachable (ConnectError) → 502.
  6. Cross-tenant tenant_id query overrides the forwarded X-Tenant-Id header.

The router under test is not yet wired into main.py, so each test builds a
dedicated app: create_app(settings) (for the full cookie/auth middleware
stack) + include_router(admin_platform_router) under the BFF api_prefix.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from agent_bff.api.v1.admin_platform import router as admin_platform_router
from agent_bff.config import Settings
from agent_bff.main import create_app

TEST_COOKIE_SECRET = "abcdefghijklmnopqrstuvwxyz0123456789ABCDEFGHIJKL"

# Downstream URLs the router reads off settings — fixed so tests can assert
# which upstream was hit.
SKILL_URL = "http://skill-test/api/skills/v1"
MCP_URL = "http://mcp-test/api/mcp/v1"
KNOW_URL = "http://know-test/api/knowledge/v1"
SCHED_URL = "http://sched-test/api/scheduler/v1"
POOL_URL = "http://pool-test/api/sandbox-pool/v1"
ORCH_URL = "http://orch-test/api/session/v1"

CALLER_TENANT = "tnt_01J5TEST00000000000000TNT1"


# ──────────────────────────────────────────────────────────────────────────────
# App / fixtures
# ──────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def settings() -> Settings:
    return Settings(
        env="test",
        log_level="WARNING",
        cookie_secret=TEST_COOKIE_SECRET,
        cookie_secure=False,
        foundation_identity_url="http://identity-test/api/identity/v1",
        session_orchestrator_url=ORCH_URL,
        skill_store_url=SKILL_URL,
        mcp_registry_url=MCP_URL,
        knowledge_ingest_url=KNOW_URL,
        sandbox_pool_url=POOL_URL,
        agent_scheduler_url=SCHED_URL,
        cors_origins="http://localhost",
        otel_enabled=False,
    )


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    app = create_app(settings)
    # The router is not yet in main.py — include it here under the same prefix.
    app.include_router(admin_platform_router, prefix=settings.api_prefix)
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture
def authed_cookie(settings: Settings) -> dict[str, str]:
    from foundation_sdk import CookieSigner, CookieSignerSettings

    signer = CookieSigner(
        CookieSignerSettings(
            secret_key=settings.cookie_secret,
            salt="agent-bff-session",
            max_age_seconds=settings.cookie_max_age,
        ),
    )
    token = signer.sign(
        {
            "user_id": "usr_01J5TEST0000000000000USER1",
            "tenant_id": CALLER_TENANT,
            "access_token": "fake-jwt-for-test",
        },
    )
    return {settings.cookie_name: token}


class FakePlatform:
    """Deterministic stub for authz + all module-7..10 downstreams.

    ``routes`` maps (METHOD, url-substring) → (status, json-body).  Tests may
    override individual routes.  Every forwarded call is recorded in
    ``requests`` as (method, url, headers) so cross-tenant header assertions
    are possible.

    Authz ``/check`` resolution
    ───────────────────────────
    By default every ``/check`` is answered with ``authz_allowed`` (the legacy
    super_admin-style stub).  When ``allowed_actions`` is supplied instead, a
    ``/check`` is allowed iff the requested ``action`` is in that set — this
    models a specific role (e.g. a ``member`` who lacks ``*.create``).  Each
    check's requested action is appended to ``checked_actions``.
    """

    def __init__(
        self,
        *,
        authz_allowed: bool = True,
        allowed_actions: set[str] | None = None,
    ) -> None:
        self.authz_allowed = authz_allowed
        self.allowed_actions = allowed_actions
        self.checked_actions: list[str] = []
        self.requests: list[tuple[str, str, dict[str, str]]] = []
        # (method, url, forwarded query params) — separate from ``requests`` so
        # existing 3-tuple unpacking stays intact.
        self.param_calls: list[tuple[str, str, dict[str, str]]] = []
        # method, url-substring → (status, body)
        self.routes: dict[tuple[str, str], tuple[int, Any]] = {
            ("GET", "/skills/"): (200, {"id": "skl_1", "name": "demo"}),
            ("GET", "/skills"): (200, {"items": [], "total": 0}),
            ("DELETE", "/skills/"): (204, None),
            ("GET", "/mcp-servers/"): (200, {"id": "mcp_1", "name": "srv"}),
            ("GET", "/mcp-servers"): (200, {"items": [], "total": 0}),
            ("POST", "/mcp-servers"): (201, {"id": "mcp_new"}),
            ("PATCH", "/mcp-servers/"): (200, {"id": "mcp_1", "enabled": False}),
            ("DELETE", "/mcp-servers/"): (204, None),
            ("GET", "/documents/"): (200, {"id": "doc_1", "title": "t"}),
            ("GET", "/documents"): (200, {"items": [], "total": 0}),
            ("DELETE", "/documents/"): (204, None),
            ("POST", "/ingest"): (200, {"document_id": "doc_1", "status": "READY"}),
            ("POST", "/cancel"): (200, {"id": "run_1", "status": "CANCELLED"}),
            ("GET", "/runs"): (200, {"items": []}),
            ("GET", "/sandboxes"): (200, {"items": []}),
            ("POST", "/stop"): (204, None),
            ("GET", "/_pool"): (200, {"warm": 0, "in_use": 0}),
            ("GET", "/token-usage/daily"): (200, {"days": []}),
        }

    def route(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        self.requests.append((method, url, dict(kwargs.get("headers") or {})))
        self.param_calls.append((method, url, dict(kwargs.get("params") or {})))

        # authz /check — the BFF guard.
        if "/check" in url:
            action = (kwargs.get("json") or {}).get("action", "")
            self.checked_actions.append(action)
            if self.allowed_actions is not None:
                allowed = action in self.allowed_actions
            else:
                allowed = self.authz_allowed
            return httpx.Response(200, json={"allowed": allowed})

        # Match against the path *after* the service base url so the base's
        # own "/skills/" / "/documents" segments don't trip the matcher.
        # Strip scheme+host+/api/<svc>/v1 prefix, keep from the first segment
        # after the version.  Simplest robust approach: match on the tail.
        tail = url.split("/v1", 1)[-1] if "/v1" in url else url

        # Longest-substring match on the tail wins so "/skills/{id}" beats
        # "/skills" and "/runs/{id}/cancel" beats "/runs".
        best: tuple[int, Any] | None = None
        best_len = -1
        for (m, sub), result in self.routes.items():
            if m == method and sub in tail and len(sub) > best_len:
                best, best_len = result, len(sub)
        if best is not None:
            status_code, body = best
            if body is None:
                return httpx.Response(status_code)
            return httpx.Response(status_code, json=body)

        return httpx.Response(200, json={"ok": True})


def _install(monkeypatch: pytest.MonkeyPatch, fake: FakePlatform) -> FakePlatform:
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
def fake_allowed(monkeypatch: pytest.MonkeyPatch) -> FakePlatform:
    return _install(monkeypatch, FakePlatform(authz_allowed=True))


@pytest.fixture
def fake_denied(monkeypatch: pytest.MonkeyPatch) -> FakePlatform:
    return _install(monkeypatch, FakePlatform(authz_allowed=False))


# The ``member`` system role's grants for these three services are read-only:
# skill.read / document.read / document.search / mcp_server.read.  Crucially it
# holds no ``*.create`` for skills/mcp/knowledge — which is the BFF gate action.
_MEMBER_ACTIONS = {
    "skill.read",
    "document.read",
    "document.search",
    "mcp_server.read",
    "run.read",
    "run.create",
    "run.cancel",
    "app.read",
}

# tenant_admin holds the wildcards covering every module action, but NOT
# ``platform.admin`` — so it passes same-tenant module gates but is barred from
# cross-tenant operations.  ``sandbox.read`` is the ops-module gate action
# (held by tenant_admin via ``sandbox.*``, not by member).
_TENANT_ADMIN_ACTIONS = {
    "skill.create",
    "mcp_server.create",
    "document.create",
    "sandbox.read",
}


@pytest.fixture
def fake_member(monkeypatch: pytest.MonkeyPatch) -> FakePlatform:
    """A plain member: holds read tokens but no ``*.create`` for the 3 modules."""
    return _install(monkeypatch, FakePlatform(allowed_actions=_MEMBER_ACTIONS))


@pytest.fixture
def fake_tenant_admin(monkeypatch: pytest.MonkeyPatch) -> FakePlatform:
    """A tenant_admin: holds module ``*.create`` but not ``platform.admin``."""
    return _install(
        monkeypatch, FakePlatform(allowed_actions=_TENANT_ADMIN_ACTIONS),
    )


# ──────────────────────────────────────────────────────────────────────────────
# Auth: every endpoint requires super_admin
# ──────────────────────────────────────────────────────────────────────────────

# (path, method, json) tuples covering every endpoint at least once.
ALL_ENDPOINTS = [
    ("/admin/skills", "GET", None),
    ("/admin/skills/skl_1", "GET", None),
    ("/admin/skills/skl_1/versions", "GET", None),
    ("/admin/skills/skl_1", "DELETE", None),
    ("/admin/mcp/servers", "GET", None),
    ("/admin/mcp/servers", "POST", {"name": "s", "transport": "stdio"}),
    ("/admin/mcp/servers/mcp_1", "GET", None),
    ("/admin/mcp/servers/mcp_1", "PATCH", {"enabled": False}),
    ("/admin/mcp/servers/mcp_1", "DELETE", None),
    ("/admin/knowledge/documents", "GET", None),
    ("/admin/knowledge/documents/doc_1", "GET", None),
    ("/admin/knowledge/documents/doc_1/reingest", "POST", None),
    ("/admin/knowledge/documents/doc_1", "DELETE", None),
    ("/admin/ops/runs", "GET", None),
    ("/admin/ops/runs/run_1/cancel", "POST", None),
    ("/admin/ops/sandboxes", "GET", None),
    ("/admin/ops/sandboxes/sb_1/stop", "POST", None),
    ("/admin/ops/pool", "GET", None),
    ("/admin/ops/usage/daily", "GET", None),
]


def _call(client: TestClient, path: str, method: str, json, cookies=None):
    url = f"/api/bff/v1{path}"
    return client.request(method, url, json=json, cookies=cookies or {})


@pytest.mark.parametrize("path,method,json", ALL_ENDPOINTS)
def test_unauthenticated_rejected(client, path, method, json) -> None:
    resp = _call(client, path, method, json)
    assert resp.status_code in (401, 403), f"{method} {path} → {resp.status_code}"


@pytest.mark.parametrize("path,method,json", ALL_ENDPOINTS)
def test_authz_denied_forbidden(client, authed_cookie, fake_denied, path, method, json) -> None:
    resp = _call(client, path, method, json, cookies=authed_cookie)
    assert resp.status_code == 403, f"{method} {path} → {resp.status_code}"


@pytest.mark.parametrize("path,method,json", ALL_ENDPOINTS)
def test_super_admin_proxied(client, authed_cookie, fake_allowed, path, method, json) -> None:
    resp = _call(client, path, method, json, cookies=authed_cookie)
    assert resp.status_code in (200, 201), f"{method} {path} → {resp.status_code}: {resp.text}"


# ──────────────────────────────────────────────────────────────────────────────
# Per-module happy-path shape + correct upstream targeting
# ──────────────────────────────────────────────────────────────────────────────

def test_skills_list_hits_skill_store(client, authed_cookie, fake_allowed) -> None:
    resp = client.get("/api/bff/v1/admin/skills", cookies=authed_cookie)
    assert resp.status_code == 200
    assert "items" in resp.json()
    hit = [u for (_, u, _) in fake_allowed.requests if f"{SKILL_URL}/skills" in u]
    assert hit, "skill-store /skills not called"


def test_mcp_create_hits_registry(client, authed_cookie, fake_allowed) -> None:
    resp = client.post(
        "/api/bff/v1/admin/mcp/servers",
        json={"name": "s", "transport": "stdio"},
        cookies=authed_cookie,
    )
    assert resp.status_code == 201
    assert resp.json()["id"] == "mcp_new"
    assert any(f"{MCP_URL}/mcp-servers" in u for (_, u, _) in fake_allowed.requests)


def test_reingest_maps_to_ingest(client, authed_cookie, fake_allowed) -> None:
    resp = client.post(
        "/api/bff/v1/admin/knowledge/documents/doc_1/reingest",
        cookies=authed_cookie,
    )
    assert resp.status_code == 200
    # BFF "reingest" must hit the downstream's /ingest route.
    assert any(
        u == f"{KNOW_URL}/documents/doc_1/ingest"
        for (_, u, _) in fake_allowed.requests
    ), "reingest did not map to /ingest"


def test_admin_documents_list_forces_include_memory(
    client, authed_cookie, fake_allowed,
) -> None:
    """The admin audit listing must ask knowledge-ingest for the full set,
    memory docs included, by forwarding include_memory=true."""
    resp = client.get(
        "/api/bff/v1/admin/knowledge/documents", cookies=authed_cookie,
    )
    assert resp.status_code == 200
    doc_params = [
        p for (m, u, p) in fake_allowed.param_calls
        if m == "GET" and u == f"{KNOW_URL}/documents"
    ]
    assert doc_params, "admin documents list did not hit knowledge-ingest /documents"
    assert doc_params[0].get("include_memory") == "true"


def test_delete_document_hits_knowledge_ingest(client, authed_cookie, fake_allowed) -> None:
    resp = client.delete(
        "/api/bff/v1/admin/knowledge/documents/doc_1", cookies=authed_cookie,
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "deleted"
    assert any(
        m == "DELETE" and u == f"{KNOW_URL}/documents/doc_1"
        for (m, u, _) in fake_allowed.requests
    ), "delete document did not hit knowledge-ingest /documents/{id}"


def test_pool_snapshot_hits_sandbox_pool(client, authed_cookie, fake_allowed) -> None:
    resp = client.get("/api/bff/v1/admin/ops/pool", cookies=authed_cookie)
    assert resp.status_code == 200
    assert any(
        u == f"{POOL_URL}/sandboxes/_pool" for (_, u, _) in fake_allowed.requests
    )


def test_usage_daily_hits_orchestrator_and_forwards_params(
    client, authed_cookie, fake_allowed,
) -> None:
    resp = client.get(
        "/api/bff/v1/admin/ops/usage/daily?days=30", cookies=authed_cookie,
    )
    assert resp.status_code == 200
    assert any(
        u == f"{ORCH_URL}/runs/token-usage/daily"
        for (_, u, _) in fake_allowed.requests
    )


def test_cancel_run_hits_scheduler(client, authed_cookie, fake_allowed) -> None:
    resp = client.post(
        "/api/bff/v1/admin/ops/runs/run_1/cancel", cookies=authed_cookie,
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "CANCELLED"
    assert any(
        u == f"{SCHED_URL}/runs/run_1/cancel" for (_, u, _) in fake_allowed.requests
    )


# ──────────────────────────────────────────────────────────────────────────────
# Cross-tenant: tenant_id query overrides forwarded X-Tenant-Id
# ──────────────────────────────────────────────────────────────────────────────

def test_tenant_id_override_changes_forwarded_header(
    client, authed_cookie, fake_allowed,
) -> None:
    target = "tnt_OTHER0000000000000000000001"
    resp = client.get(
        f"/api/bff/v1/admin/skills?tenant_id={target}", cookies=authed_cookie,
    )
    assert resp.status_code == 200
    skill_reqs = [
        h for (_, u, h) in fake_allowed.requests if f"{SKILL_URL}/skills" in u
    ]
    assert skill_reqs and skill_reqs[0].get("X-Tenant-Id") == target


def test_tenant_id_absent_uses_caller_tenant(
    client, authed_cookie, fake_allowed,
) -> None:
    resp = client.get("/api/bff/v1/admin/skills", cookies=authed_cookie)
    assert resp.status_code == 200
    skill_reqs = [
        h for (_, u, h) in fake_allowed.requests if f"{SKILL_URL}/skills" in u
    ]
    assert skill_reqs and skill_reqs[0].get("X-Tenant-Id") == CALLER_TENANT


def test_tenant_id_not_forwarded_as_query_param(
    client, authed_cookie, fake_allowed,
) -> None:
    """The BFF-private tenant_id override must not leak into downstream params."""
    target = "tnt_OTHER0000000000000000000001"
    resp = client.get(
        f"/api/bff/v1/admin/ops/usage/daily?tenant_id={target}&days=14",
        cookies=authed_cookie,
    )
    assert resp.status_code == 200
    # The forwarded URL is recorded without query string by httpx stub (params
    # passed separately), so assert the override landed in the header instead.
    usage_reqs = [
        h for (_, u, h) in fake_allowed.requests
        if u == f"{ORCH_URL}/runs/token-usage/daily"
    ]
    assert usage_reqs and usage_reqs[0].get("X-Tenant-Id") == target


# ──────────────────────────────────────────────────────────────────────────────
# Upstream error透传 + unreachable → 502
# ──────────────────────────────────────────────────────────────────────────────

def test_upstream_4xx_passed_through(
    client, authed_cookie, monkeypatch,
) -> None:
    fake = FakePlatform(authz_allowed=True)
    fake.routes[("GET", "/skills/")] = (404, {"detail": "skill not found"})
    _install(monkeypatch, fake)
    resp = client.get("/api/bff/v1/admin/skills/missing", cookies=authed_cookie)
    assert resp.status_code == 404
    assert resp.json()["detail"]["detail"] == "skill not found"


def test_upstream_5xx_passed_through(
    client, authed_cookie, monkeypatch,
) -> None:
    fake = FakePlatform(authz_allowed=True)
    fake.routes[("GET", "/mcp-servers")] = (503, {"detail": "registry down"})
    _install(monkeypatch, fake)
    resp = client.get("/api/bff/v1/admin/mcp/servers", cookies=authed_cookie)
    assert resp.status_code == 503


def test_upstream_unreachable_is_502(
    client, authed_cookie, monkeypatch,
) -> None:
    """A ConnectError on a downstream leg → readable 502 (not 500)."""

    class _Broken:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None

        async def post(self, url, **kw):
            # authz /check must still succeed so the guard passes.
            if "/check" in url:
                return httpx.Response(200, json={"allowed": True})
            raise httpx.ConnectError("down")

        async def get(self, url, **kw):
            if "/check" in url:
                return httpx.Response(200, json={"allowed": True})
            raise httpx.ConnectError("down")

        async def patch(self, url, **kw):
            raise httpx.ConnectError("down")

        async def delete(self, url, **kw):
            raise httpx.ConnectError("down")

    monkeypatch.setattr(httpx, "AsyncClient", _Broken)
    resp = client.get("/api/bff/v1/admin/skills", cookies=authed_cookie)
    assert resp.status_code == 502


# ──────────────────────────────────────────────────────────────────────────────
# Tenant delegation — skills / mcp / knowledge are gated on the module's
# ``*.create`` action (held by tenant_admin, not member).
# ──────────────────────────────────────────────────────────────────────────────

# Each tenant-delegated endpoint paired with the module-create action the BFF
# guard authorizes for it.
DELEGATED_ENDPOINTS = [
    ("/admin/skills", "GET", None, "skill.create"),
    ("/admin/skills/skl_1", "GET", None, "skill.create"),
    ("/admin/skills/skl_1/versions", "GET", None, "skill.create"),
    ("/admin/skills/skl_1", "DELETE", None, "skill.create"),
    ("/admin/mcp/servers", "GET", None, "mcp_server.create"),
    ("/admin/mcp/servers", "POST", {"name": "s", "transport": "stdio"}, "mcp_server.create"),
    ("/admin/mcp/servers/mcp_1", "GET", None, "mcp_server.create"),
    ("/admin/mcp/servers/mcp_1", "PATCH", {"enabled": False}, "mcp_server.create"),
    ("/admin/mcp/servers/mcp_1", "DELETE", None, "mcp_server.create"),
    ("/admin/knowledge/documents", "GET", None, "document.create"),
    ("/admin/knowledge/documents/doc_1", "GET", None, "document.create"),
    ("/admin/knowledge/documents/doc_1/reingest", "POST", None, "document.create"),
    ("/admin/knowledge/documents/doc_1", "DELETE", None, "document.create"),
]

# Ops runs / sandboxes / usage are now delegated to tenant admins, gated on
# ``sandbox.read`` and forwarding WITHOUT X-Actor:ADMIN (scheduler re-authorizes
# the real caller).  The sandbox-pool snapshot stays platform-super-admin only.
DELEGATED_OPS_ENDPOINTS = [
    ("/admin/ops/runs", "GET", None, "sandbox.read"),
    ("/admin/ops/runs/run_1/cancel", "POST", None, "sandbox.read"),
    ("/admin/ops/sandboxes", "GET", None, "sandbox.read"),
    ("/admin/ops/sandboxes/sb_1/stop", "POST", None, "sandbox.read"),
    ("/admin/ops/usage/daily", "GET", None, "sandbox.read"),
]

# The sandbox-pool snapshot is platform-level infra: still super_admin-only and
# still asserts X-Actor:ADMIN downstream.
POOL_ENDPOINTS = [
    ("/admin/ops/pool", "GET", None),
]


# Skills/MCP/Knowledge + ops runs/sandboxes/usage are all tenant-delegated:
# gated on a module action held by tenant_admin (not member) and forwarded
# without X-Actor:ADMIN.
ALL_DELEGATED_ENDPOINTS = DELEGATED_ENDPOINTS + DELEGATED_OPS_ENDPOINTS


@pytest.mark.parametrize("path,method,json,action", ALL_DELEGATED_ENDPOINTS)
def test_tenant_admin_allowed_on_delegated_modules(
    client, authed_cookie, fake_tenant_admin, path, method, json, action,
) -> None:
    """tenant_admin (holds the module gate action, no platform.admin) is allowed."""
    resp = _call(client, path, method, json, cookies=authed_cookie)
    assert resp.status_code in (200, 201), f"{method} {path} → {resp.status_code}"
    # The BFF authorized the module's gate action (same-tenant lane).
    assert action in fake_tenant_admin.checked_actions


@pytest.mark.parametrize("path,method,json,action", ALL_DELEGATED_ENDPOINTS)
def test_member_denied_on_delegated_modules(
    client, authed_cookie, fake_member, path, method, json, action,
) -> None:
    """A plain member (read-only grants, no module gate action) is 403'd at the BFF.

    For ops this is the key downscope assertion: member holds ``run.*`` but NOT
    ``sandbox.read``, so it cannot reach runs/sandboxes/usage.
    """
    resp = _call(client, path, method, json, cookies=authed_cookie)
    assert resp.status_code == 403, f"{method} {path} → {resp.status_code}"


@pytest.mark.parametrize("path,method,json,action", ALL_DELEGATED_ENDPOINTS)
def test_delegated_modules_do_not_forward_admin_actor(
    client, authed_cookie, fake_tenant_admin, path, method, json, action,
) -> None:
    """Delegated modules must forward WITHOUT X-Actor:ADMIN so the upstream
    service re-authorizes the real caller per action (no bypass)."""
    resp = _call(client, path, method, json, cookies=authed_cookie)
    assert resp.status_code in (200, 201)
    # Inspect every forwarded (non-/check) call's headers.
    forwarded = [
        h for (_, u, h) in fake_tenant_admin.requests if "/check" not in u
    ]
    assert forwarded, "no upstream call recorded"
    for hdrs in forwarded:
        assert "X-Actor" not in hdrs, f"{method} {path} leaked X-Actor: {hdrs}"


@pytest.mark.parametrize("path,method,json", POOL_ENDPOINTS)
def test_pool_endpoint_still_requires_super_admin(
    client, authed_cookie, fake_member, path, method, json,
) -> None:
    """The sandbox-pool snapshot is unchanged: a non-platform-admin (member,
    even with run.*) is 403'd — it lacks platform.admin."""
    resp = _call(client, path, method, json, cookies=authed_cookie)
    assert resp.status_code == 403, f"{method} {path} → {resp.status_code}"


@pytest.mark.parametrize("path,method,json", POOL_ENDPOINTS)
def test_pool_endpoint_still_forwards_admin_actor(
    client, authed_cookie, fake_allowed, path, method, json,
) -> None:
    """The sandbox-pool snapshot still asserts X-Actor:ADMIN downstream."""
    resp = _call(client, path, method, json, cookies=authed_cookie)
    assert resp.status_code in (200, 201, 204)
    forwarded = [h for (_, u, h) in fake_allowed.requests if "/check" not in u]
    assert forwarded, "no upstream call recorded"
    assert all(h.get("X-Actor") == "ADMIN" for h in forwarded)


# ──────────────────────────────────────────────────────────────────────────────
# Cross-tenant gate — a tenant_admin passing a foreign ?tenant_id= is 403'd;
# a super_admin (platform.admin) sails through.
# ──────────────────────────────────────────────────────────────────────────────

def test_tenant_admin_cross_tenant_forbidden(
    client, authed_cookie, fake_tenant_admin,
) -> None:
    """tenant_admin (no platform.admin) targeting a foreign tenant → 403."""
    foreign = "tnt_OTHER0000000000000000000001"
    resp = client.get(
        f"/api/bff/v1/admin/skills?tenant_id={foreign}", cookies=authed_cookie,
    )
    assert resp.status_code == 403
    # The cross-tenant lane checked platform.admin (and the member/admin set
    # does not grant it), so no skill-store call should have been forwarded.
    assert not any(
        f"{SKILL_URL}/skills" in u for (_, u, _) in fake_tenant_admin.requests
    )


def test_super_admin_cross_tenant_allowed_no_admin_actor(
    client, authed_cookie, fake_allowed,
) -> None:
    """super_admin (platform.admin via authz_allowed) may target any tenant; the
    forwarded X-Tenant-Id is the override and still no X-Actor:ADMIN is sent."""
    target = "tnt_OTHER0000000000000000000001"
    resp = client.get(
        f"/api/bff/v1/admin/knowledge/documents?tenant_id={target}",
        cookies=authed_cookie,
    )
    assert resp.status_code == 200
    doc_reqs = [
        h for (_, u, h) in fake_allowed.requests if f"{KNOW_URL}/documents" in u
    ]
    assert doc_reqs and doc_reqs[0].get("X-Tenant-Id") == target
    assert "X-Actor" not in doc_reqs[0]
