"""Tests: BFF RBAC guard split on the LLM model-management surface.

The model-management surface is split by *scope*:

  • Tenant-scoped (delegated to tenant administrators) — guarded by
    ``require_tenant_rbac_bff`` in ``admin_model.py``:
      - GET  /admin/llm/providers            → model.read (read catalogue)
      - GET  /admin/llm/tenant-defaults       → model.read
      - GET  /admin/llm/tenant-defaults/resolve → model.read
      - PUT  /admin/llm/tenant-defaults       → model.admin
  • Platform-scoped (super_admin only) — guarded by ``require_super_admin_bff``
    in ``admin.py``:
      - POST   /admin/llm/probe               → platform.admin
      - POST   /admin/llm/providers           → platform.admin
      - PATCH  /admin/llm/providers/{pid}     → platform.admin
      - DELETE /admin/llm/providers/{pid}     → platform.admin

These tests drive the real proxy routes with an action-aware authz stub,
asserting:

  1. tenant_admin (model.read) reads providers / tenant-defaults / resolve → 200
  2. tenant_admin (model.admin) writes tenant-defaults → 200, X-Actor: ADMIN
     forwarded (gateway requires it; tenant-scoped, not RLS bypass)
  3. model.read does NOT open the write surface: a holder of only model.read is
     403'd on PUT tenant-defaults (which demands model.admin) and on every
     platform endpoint (provider writes / probe, which demand platform.admin)
  4. member (no model.* tokens) → 403 on every tenant-scoped read, upstream
     never called
  5. provider writes + probe stay super_admin-only: model.admin does NOT open
     them (platform.admin required)
  6. super_admin (``*`` → platform.admin) retains full access (no regression)
  7. cross-tenant: tenant_admin reading a foreign ?tenant_id= → 403; super_admin
     → allowed
  8. authz /check unreachable → fail-closed 403
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
def model_client(settings: Settings) -> Iterator[TestClient]:
    app = create_app(settings)
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


class FakeModelGateway:
    """Action-aware authz /check stub + llm-gateway upstream responses.

    ``granted`` is the set of actions /check returns allowed=True for; every
    other action → allowed=False.  Records the action + X-Tenant-Id of each
    /check, and every upstream (non-check) call with its forwarded headers, so
    tests can assert lane selection, the X-Actor: ADMIN header, and the
    cross-tenant override.
    """

    def __init__(self, *, granted: set[str]) -> None:
        self.granted = granted
        self.check_actions: list[str] = []
        self.check_tenants: list[str] = []
        self.upstream_calls: list[tuple[str, str]] = []
        self.upstream_headers: list[dict[str, str]] = []

    def route(self, method: str, url: str, **kw: Any) -> httpx.Response:
        # identity /sessions/me — used by require_super_admin_bff's role probe
        # path is not exercised here (it uses /check), but keep a safe default.
        if "/check" in url:
            body = kw.get("json") or {}
            action = body.get("action", "")
            self.check_actions.append(action)
            self.check_tenants.append((kw.get("headers") or {}).get("X-Tenant-Id", ""))
            return httpx.Response(200, json={"allowed": action in self.granted})

        # Upstream llm-gateway.
        self.upstream_calls.append((method, url))
        self.upstream_headers.append(dict(kw.get("headers") or {}))
        if "/providers" in url and method == "GET":
            return httpx.Response(200, json={"items": [
                {"id": "prov_1", "name": "OpenAI", "enabled": True},
            ]})
        if "/providers" in url and method == "POST":
            return httpx.Response(201, json={"id": "prov_new"})
        if "/providers" in url and method in ("PATCH", "DELETE"):
            return httpx.Response(200, json={"id": "prov_1"})
        if "/tenant-defaults/resolve" in url:
            return httpx.Response(200, json={"purpose": "llm", "model_id": "gpt-4o"})
        if "/tenant-defaults" in url and method == "GET":
            return httpx.Response(200, json={"items": [{"purpose": "llm", "model_id": "gpt-4o"}]})
        if "/tenant-defaults" in url and method == "PUT":
            return httpx.Response(200, json={"purpose": "llm", "model_id": "gpt-4o"})
        if "/probe" in url:
            return httpx.Response(200, json={"url": "http://x", "results": {}})
        return httpx.Response(200, json={"ok": True})


def _install(fake: FakeModelGateway, monkeypatch: pytest.MonkeyPatch) -> FakeModelGateway:
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


def _install_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    class _AC:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        async def get(self, url, **kw): raise httpx.ConnectError("authz down")
        async def post(self, url, **kw): raise httpx.ConnectError("authz down")
        async def put(self, url, **kw): raise httpx.ConnectError("authz down")
        async def patch(self, url, **kw): raise httpx.ConnectError("authz down")
        async def delete(self, url, **kw): raise httpx.ConnectError("authz down")

    monkeypatch.setattr(httpx, "AsyncClient", _AC)


# ── Same-tenant lane: tenant_admin (model.read) reads ─────────────────────────

def test_tenant_admin_can_read_providers(
    model_client: TestClient, authed_cookie: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """tenant_admin holding model.read → GET providers proxied (200)."""
    fake = _install(FakeModelGateway(granted={"model.read"}), monkeypatch)
    resp = model_client.get(PREFIX + "/admin/llm/providers", cookies=authed_cookie)
    assert resp.status_code == 200
    assert fake.check_actions == ["model.read"]
    assert fake.upstream_calls and "/providers" in fake.upstream_calls[0][1]


def test_tenant_admin_can_read_tenant_defaults(
    model_client: TestClient, authed_cookie: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GET tenant-defaults gates on model.read."""
    fake = _install(FakeModelGateway(granted={"model.read"}), monkeypatch)
    resp = model_client.get(
        PREFIX + "/admin/llm/tenant-defaults", cookies=authed_cookie,
    )
    assert resp.status_code == 200
    assert fake.check_actions == ["model.read"]


def test_tenant_admin_can_resolve_tenant_defaults(
    model_client: TestClient, authed_cookie: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GET tenant-defaults/resolve gates on model.read."""
    fake = _install(FakeModelGateway(granted={"model.read"}), monkeypatch)
    resp = model_client.get(
        PREFIX + "/admin/llm/tenant-defaults/resolve?purpose=llm", cookies=authed_cookie,
    )
    assert resp.status_code == 200
    assert fake.check_actions == ["model.read"]


# ── Same-tenant lane: tenant_admin (model.admin) writes ───────────────────────

def test_tenant_admin_can_write_tenant_defaults_with_x_actor_admin(
    model_client: TestClient, authed_cookie: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PUT tenant-defaults gates on model.admin AND forwards X-Actor: ADMIN.

    The gateway requires X-Actor in {ADMIN, SERVICE} to write tenant-defaults; it
    scopes the write to the caller's own tenant, so the ADMIN actor is
    tenant-scoped (not an RLS bypass).  The BFF guard is the cross-tenant gate.
    """
    fake = _install(FakeModelGateway(granted={"model.admin"}), monkeypatch)
    resp = model_client.put(
        PREFIX + "/admin/llm/tenant-defaults",
        json={"purpose": "llm", "model_id": "gpt-4o", "provider_id": "prov_1"},
        cookies=authed_cookie,
    )
    assert resp.status_code == 200
    assert fake.check_actions == ["model.admin"]
    put_calls = [(m, u) for (m, u) in fake.upstream_calls if m == "PUT"]
    assert put_calls and "/tenant-defaults" in put_calls[0][1]
    put_headers = [h for (m, u), h in zip(fake.upstream_calls, fake.upstream_headers) if m == "PUT"]
    assert put_headers and put_headers[0].get("X-Actor") == "ADMIN"


# ── model.read must NOT open the write surface ────────────────────────────────

def test_model_read_does_not_open_tenant_defaults_write(
    model_client: TestClient, authed_cookie: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A holder of only model.read is 403'd on PUT tenant-defaults (needs model.admin)."""
    fake = _install(FakeModelGateway(granted={"model.read"}), monkeypatch)
    resp = model_client.put(
        PREFIX + "/admin/llm/tenant-defaults",
        json={"purpose": "llm", "model_id": "gpt-4o", "provider_id": "prov_1"},
        cookies=authed_cookie,
    )
    assert resp.status_code == 403
    assert fake.check_actions == ["model.admin"]
    assert fake.upstream_calls == []


# ── member (no model tokens) → 403 on every tenant-scoped read ────────────────

def test_member_cannot_read_providers(
    model_client: TestClient, authed_cookie: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _install(FakeModelGateway(granted=set()), monkeypatch)
    resp = model_client.get(PREFIX + "/admin/llm/providers", cookies=authed_cookie)
    assert resp.status_code == 403
    assert fake.check_actions == ["model.read"]
    assert fake.upstream_calls == []


def test_member_cannot_read_tenant_defaults(
    model_client: TestClient, authed_cookie: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _install(FakeModelGateway(granted=set()), monkeypatch)
    resp = model_client.get(PREFIX + "/admin/llm/tenant-defaults", cookies=authed_cookie)
    assert resp.status_code == 403
    assert fake.upstream_calls == []


def test_member_cannot_write_tenant_defaults(
    model_client: TestClient, authed_cookie: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _install(FakeModelGateway(granted=set()), monkeypatch)
    resp = model_client.put(
        PREFIX + "/admin/llm/tenant-defaults",
        json={"purpose": "llm", "model_id": "gpt-4o", "provider_id": "prov_1"},
        cookies=authed_cookie,
    )
    assert resp.status_code == 403
    assert fake.check_actions == ["model.admin"]
    assert fake.upstream_calls == []


# ── Provider writes + probe stay super_admin-only ─────────────────────────────

def test_model_admin_does_not_open_provider_create(
    model_client: TestClient, authed_cookie: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POST /providers is super_admin-only; a model.admin holder → 403."""
    fake = _install(FakeModelGateway(granted={"model.admin"}), monkeypatch)
    resp = model_client.post(
        PREFIX + "/admin/llm/providers",
        json={"name": "OpenAI", "base_url": "http://x"},
        cookies=authed_cookie,
    )
    assert resp.status_code == 403
    # super_admin guard probes platform.admin (not model.admin).
    assert fake.check_actions == ["platform.admin"]
    assert fake.upstream_calls == []


def test_model_admin_does_not_open_provider_update(
    model_client: TestClient, authed_cookie: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _install(FakeModelGateway(granted={"model.admin"}), monkeypatch)
    resp = model_client.patch(
        PREFIX + "/admin/llm/providers/prov_1",
        json={"enabled": False},
        cookies=authed_cookie,
    )
    assert resp.status_code == 403
    assert fake.check_actions == ["platform.admin"]
    assert fake.upstream_calls == []


def test_model_admin_does_not_open_provider_delete(
    model_client: TestClient, authed_cookie: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _install(FakeModelGateway(granted={"model.admin"}), monkeypatch)
    resp = model_client.delete(
        PREFIX + "/admin/llm/providers/prov_1", cookies=authed_cookie,
    )
    assert resp.status_code == 403
    assert fake.check_actions == ["platform.admin"]
    assert fake.upstream_calls == []


def test_model_admin_does_not_open_probe(
    model_client: TestClient, authed_cookie: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POST /probe is super_admin-only; a model.admin holder → 403."""
    fake = _install(FakeModelGateway(granted={"model.admin"}), monkeypatch)
    resp = model_client.post(
        PREFIX + "/admin/llm/probe",
        json={"url": "http://x", "api_key": "sk-x"},
        cookies=authed_cookie,
    )
    assert resp.status_code == 403
    assert fake.check_actions == ["platform.admin"]
    assert fake.upstream_calls == []


# ── super_admin retains full access (no regression) ───────────────────────────

def test_super_admin_can_read_providers(
    model_client: TestClient, authed_cookie: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """super_admin (platform.admin via *) still reads providers (model.read lane)."""
    fake = _install(FakeModelGateway(granted={"model.read", "platform.admin"}), monkeypatch)
    resp = model_client.get(PREFIX + "/admin/llm/providers", cookies=authed_cookie)
    assert resp.status_code == 200


def test_super_admin_can_create_provider(
    model_client: TestClient, authed_cookie: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _install(FakeModelGateway(granted={"platform.admin"}), monkeypatch)
    resp = model_client.post(
        PREFIX + "/admin/llm/providers",
        json={"name": "OpenAI", "base_url": "http://x"},
        cookies=authed_cookie,
    )
    assert resp.status_code == 201
    assert fake.check_actions == ["platform.admin"]
    assert fake.upstream_calls and fake.upstream_calls[0][0] == "POST"


def test_super_admin_can_probe(
    model_client: TestClient, authed_cookie: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _install(FakeModelGateway(granted={"platform.admin"}), monkeypatch)
    resp = model_client.post(
        PREFIX + "/admin/llm/probe",
        json={"url": "http://x", "api_key": "sk-x"},
        cookies=authed_cookie,
    )
    assert resp.status_code == 200
    assert fake.check_actions == ["platform.admin"]


def test_super_admin_can_write_tenant_defaults(
    model_client: TestClient, authed_cookie: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """super_admin write tenant-defaults still forwards X-Actor: ADMIN."""
    fake = _install(FakeModelGateway(granted={"model.admin", "platform.admin"}), monkeypatch)
    resp = model_client.put(
        PREFIX + "/admin/llm/tenant-defaults",
        json={"purpose": "llm", "model_id": "gpt-4o", "provider_id": "prov_1"},
        cookies=authed_cookie,
    )
    assert resp.status_code == 200
    put_headers = [h for (m, u), h in zip(fake.upstream_calls, fake.upstream_headers) if m == "PUT"]
    assert put_headers and put_headers[0].get("X-Actor") == "ADMIN"


# ── Cross-tenant lane ─────────────────────────────────────────────────────────

def test_cross_tenant_tenant_admin_denied(
    model_client: TestClient, authed_cookie: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """tenant_admin (model.read, no platform.admin) reading a foreign tenant → 403."""
    fake = _install(FakeModelGateway(granted={"model.read"}), monkeypatch)
    resp = model_client.get(
        PREFIX + f"/admin/llm/providers?tenant_id={OTHER_TENANT}", cookies=authed_cookie,
    )
    assert resp.status_code == 403
    # Cross-tenant lane probes platform.admin against the caller's OWN tenant.
    assert fake.check_actions == ["platform.admin"]
    assert fake.check_tenants == [OWN_TENANT]
    assert fake.upstream_calls == []


def test_cross_tenant_super_admin_allowed(
    model_client: TestClient, authed_cookie: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """super_admin (platform.admin) may read providers for a foreign tenant."""
    fake = _install(FakeModelGateway(granted={"platform.admin"}), monkeypatch)
    resp = model_client.get(
        PREFIX + f"/admin/llm/providers?tenant_id={OTHER_TENANT}", cookies=authed_cookie,
    )
    assert resp.status_code == 200
    assert fake.check_actions == ["platform.admin"]
    assert fake.upstream_calls and "/providers" in fake.upstream_calls[0][1]


# ── Fail-closed transport + unauthenticated ───────────────────────────────────

def test_authz_unreachable_is_403(
    model_client: TestClient, authed_cookie: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """authz /check transport failure on a tenant-scoped read → fail-closed 403."""
    _install_unreachable(monkeypatch)
    resp = model_client.get(PREFIX + "/admin/llm/providers", cookies=authed_cookie)
    assert resp.status_code == 403


def test_unauthenticated_rejected(model_client: TestClient) -> None:
    """No cookie → 401/403 before any authz call."""
    resp = model_client.get(PREFIX + "/admin/llm/providers")
    assert resp.status_code in (401, 403)
