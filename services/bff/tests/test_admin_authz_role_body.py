"""Tests: BFF role create/update body whitelisting (anti-corruption layer).

Upstream foundation-authz ``CreateRoleRequest`` (and the role merge-patch body)
are ``extra="forbid"``: any unknown key — notably the front-end's legacy
``scope`` — yields a 422 on every call.  ``admin_authz.create_role`` /
``update_role`` therefore project the incoming body onto the exact keys upstream
accepts before forwarding.

These tests drive the real ``POST`` / ``PATCH /admin/authz/roles`` routes with
an authz stub that (a) allows ``authz.role.manage`` so the guard passes and
(b) captures the body forwarded upstream, asserting:

  1. create: ``scope`` is stripped; ``name`` / ``display_name`` / ``description``
     / ``permissions`` pass through.
  2. update: ``scope`` and ``name`` are stripped; ``display_name`` /
     ``description`` / ``permissions`` pass through.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

PREFIX = "/api/bff/v1"


class FakeAuthz:
    """authz /check stub (grants role.manage) + captures forwarded role bodies."""

    def __init__(self) -> None:
        self.captured: list[tuple[str, dict | None]] = []

    def route(self, method: str, url: str, **kw: Any) -> httpx.Response:
        if url.endswith("/check"):
            return httpx.Response(200, json={"allowed": True})
        # Upstream role REST mirror — record the forwarded body.
        self.captured.append((method, kw.get("json")))
        return httpx.Response(
            200,
            json={
                "id": "rol_01J5TEST000000000000000RL1",
                "name": "kb_editor",
                "display_name": "知识库编辑员",
                "permissions": [],
            },
        )


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


def _forwarded_body(fake: FakeAuthz, method: str) -> dict:
    """Return the JSON body forwarded to upstream for the given HTTP method."""
    for m, body in fake.captured:
        if m == method:
            assert isinstance(body, dict)
            return body
    raise AssertionError(f"no upstream {method} call captured")


def test_create_role_strips_scope_keeps_display_name(
    client: TestClient, authed_cookie: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POST: legacy ``scope`` dropped; name/display_name/description/permissions kept."""
    fake = _install(FakeAuthz(), monkeypatch)
    resp = client.post(
        PREFIX + "/admin/authz/roles",
        json={
            "name": "kb_editor",
            "display_name": "知识库编辑员",
            "description": "edits the KB",
            "scope": "TENANT",  # legacy/extra — must NOT reach upstream
            "permissions": ["document.update"],
        },
        cookies=authed_cookie,
    )
    assert resp.status_code == 201
    body = _forwarded_body(fake, "POST")
    assert "scope" not in body
    assert body == {
        "name": "kb_editor",
        "display_name": "知识库编辑员",
        "description": "edits the KB",
        "permissions": ["document.update"],
    }


def test_create_role_omits_absent_optional_keys(
    client: TestClient, authed_cookie: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POST: absent optional keys (description) are simply not forwarded."""
    fake = _install(FakeAuthz(), monkeypatch)
    resp = client.post(
        PREFIX + "/admin/authz/roles",
        json={"name": "kb_editor", "display_name": "知识库编辑员", "permissions": []},
        cookies=authed_cookie,
    )
    assert resp.status_code == 201
    body = _forwarded_body(fake, "POST")
    assert body == {"name": "kb_editor", "display_name": "知识库编辑员", "permissions": []}


def test_update_role_strips_scope_and_name(
    client: TestClient, authed_cookie: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PATCH: ``scope`` and (immutable) ``name`` dropped; merge-patch fields kept."""
    fake = _install(FakeAuthz(), monkeypatch)
    resp = client.patch(
        PREFIX + "/admin/authz/roles/rol_01J5TEST000000000000000RL1",
        json={
            "name": "renamed",  # immutable upstream — must NOT reach upstream
            "scope": "TENANT",  # extra — must NOT reach upstream
            "display_name": "新显示名",
            "description": "updated",
            "permissions": ["document.read"],
        },
        cookies=authed_cookie,
    )
    assert resp.status_code == 200
    body = _forwarded_body(fake, "PATCH")
    assert "scope" not in body
    assert "name" not in body
    assert body == {
        "display_name": "新显示名",
        "description": "updated",
        "permissions": ["document.read"],
    }
