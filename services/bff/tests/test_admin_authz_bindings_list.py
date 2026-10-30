"""Tests: BFF ``GET /admin/authz/bindings`` query translation + lifecycle fields.

foundation-authz ``GET /bindings`` filters on ``filter.principal_kind`` /
``filter.principal_id`` / ``filter.role_id`` / ``filter.status`` and silently
ignores any other filter.  It lists ACTIVE bindings only unless
``filter.status`` (comma-separated ACTIVE / EXPIRED / REVOKED) asks for more,
and every binding carries ``status`` + ``revoked_at`` / ``revoked_by`` /
``revoked_reason``.

These tests drive the real route with an authz stub that (a) allows
``authz.binding.manage`` so the guard passes and (b) captures the query params
forwarded upstream, asserting:

  1. the front-end ``filter.subject_*`` keys reach upstream as
     ``filter.principal_*`` (role filter, cursor and limit keep their names);
  2. no ``filter.status`` is sent unless asked for (upstream default: ACTIVE);
  3. ``filter.status`` is forwarded, a repeated one folded into the
     comma-separated form;
  4. the lifecycle fields and ``next_cursor`` reach the SPA;
  5. upstream's 422 for an invalid status is surfaced, not swallowed.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

PREFIX = "/api/bff/v1"
OWN_TENANT = "tnt_01J5TEST00000000000000TNT1"  # matches authed_cookie's tenant
_ROLE_ID = "rol_01J5TEST0000000000000ROLE1"
_BND_ACTIVE = "rbd_01J5TEST00000000000000BND1"
_BND_REVOKED = "rbd_01J5TEST00000000000000BND2"


def _upstream_binding(binding_id: str, **overrides: Any) -> dict:
    return {
        "id": binding_id,
        "tenant_id": OWN_TENANT,
        "principal_kind": "USER",
        "principal_id": "usr_alice",
        "role_id": _ROLE_ID,
        "scope_kind": "TENANT",
        "scope_id": None,
        "granted_at": "2026-06-13T00:00:00Z",
        "expires_at": None,
        "granted_by": "usr_admin",
        "grant_reason": "promotion",
        "revoked_at": None,
        "revoked_by": None,
        "revoked_reason": None,
        "status": "ACTIVE",
        **overrides,
    }


class FakeAuthz:
    """authz /check stub (grants binding.manage) + captures list-bindings params."""

    def __init__(self, page: dict | None = None, *, list_status: int = 200) -> None:
        self.page = page or {"items": [], "next_cursor": None}
        self.list_status = list_status
        self.list_params: list[dict] = []

    def route(self, method: str, url: str, **kw: Any) -> httpx.Response:
        if url.endswith("/check"):
            return httpx.Response(200, json={"allowed": True})
        self.list_params.append(dict(kw.get("params") or {}))
        if self.list_status != 200:
            return httpx.Response(
                self.list_status,
                json={
                    "title": "Validation failed",
                    "status": self.list_status,
                    "code": "FOUNDATION.AUTHZ.INPUT.VALIDATION_FAILED",
                },
            )
        return httpx.Response(200, json=self.page)


def _install(fake: FakeAuthz, monkeypatch: pytest.MonkeyPatch) -> FakeAuthz:
    class _AC:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        async def get(self, url, **kw): return fake.route("GET", url, **kw)
        async def post(self, url, **kw): return fake.route("POST", url, **kw)

    monkeypatch.setattr(httpx, "AsyncClient", _AC)
    return fake


def _forwarded_params(fake: FakeAuthz) -> dict:
    """The query params of the single upstream ``GET /bindings`` call."""
    assert len(fake.list_params) == 1, fake.list_params
    return fake.list_params[0]


def test_subject_filters_reach_upstream_as_principal_filters(
    client: TestClient, authed_cookie: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """filter.subject_* → filter.principal_*; role/cursor/limit pass; tenant_id is BFF-only."""
    fake = _install(FakeAuthz(), monkeypatch)
    resp = client.get(
        PREFIX + "/admin/authz/bindings",
        params={
            "tenant_id": OWN_TENANT,
            "filter.subject_type": "USER",
            "filter.subject_id": "usr_alice",
            "filter.role_id": _ROLE_ID,
            "cursor": _BND_ACTIVE,
            "limit": "50",
        },
        cookies=authed_cookie,
    )
    assert resp.status_code == 200, resp.text
    assert _forwarded_params(fake) == {
        "filter.principal_kind": "USER",
        "filter.principal_id": "usr_alice",
        "filter.role_id": _ROLE_ID,
        "cursor": _BND_ACTIVE,
        "limit": "50",
    }


def test_no_status_filter_by_default(
    client: TestClient, authed_cookie: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without filter.status the BFF sends none — upstream lists ACTIVE only."""
    fake = _install(FakeAuthz(), monkeypatch)
    resp = client.get(PREFIX + "/admin/authz/bindings", cookies=authed_cookie)
    assert resp.status_code == 200, resp.text
    assert "filter.status" not in _forwarded_params(fake)


def test_status_filter_forwarded(
    client: TestClient, authed_cookie: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _install(FakeAuthz(), monkeypatch)
    resp = client.get(
        PREFIX + "/admin/authz/bindings",
        params={"filter.status": "ACTIVE,REVOKED,EXPIRED"},
        cookies=authed_cookie,
    )
    assert resp.status_code == 200, resp.text
    assert _forwarded_params(fake) == {"filter.status": "ACTIVE,REVOKED,EXPIRED"}


def test_repeated_status_filter_folded_into_comma_form(
    client: TestClient, authed_cookie: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """?filter.status=ACTIVE&filter.status=REVOKED must not keep only the last value."""
    fake = _install(FakeAuthz(), monkeypatch)
    resp = client.get(
        PREFIX + "/admin/authz/bindings",
        params=[("filter.status", "ACTIVE"), ("filter.status", "REVOKED")],
        cookies=authed_cookie,
    )
    assert resp.status_code == 200, resp.text
    assert _forwarded_params(fake)["filter.status"] == "ACTIVE,REVOKED"


def test_lifecycle_fields_and_cursor_reach_the_spa(
    client: TestClient, authed_cookie: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = {
        "items": [
            _upstream_binding(_BND_ACTIVE),
            _upstream_binding(
                _BND_REVOKED,
                status="REVOKED",
                revoked_at="2026-10-01T09:30:00Z",
                revoked_by="usr_admin",
                revoked_reason="API revoke",
            ),
        ],
        "next_cursor": _BND_REVOKED,
    }
    _install(FakeAuthz(page), monkeypatch)
    resp = client.get(
        PREFIX + "/admin/authz/bindings",
        params={"filter.status": "ACTIVE,REVOKED"},
        cookies=authed_cookie,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["next_cursor"] == _BND_REVOKED
    active, revoked = body["items"]
    assert active["status"] == "ACTIVE"
    assert active["revoked_at"] is None
    assert revoked["status"] == "REVOKED"
    assert revoked["revoked_at"] == "2026-10-01T09:30:00Z"
    assert revoked["revoked_by"] == "usr_admin"
    assert revoked["revoked_reason"] == "API revoke"
    # Still translated to the front-end vocabulary.
    assert revoked["subject_id"] == "usr_alice"
    assert "principal_id" not in revoked


def test_invalid_status_422_is_surfaced(
    client: TestClient, authed_cookie: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The BFF doesn't validate statuses itself; upstream's 422 reaches the caller."""
    fake = _install(FakeAuthz(list_status=422), monkeypatch)
    resp = client.get(
        PREFIX + "/admin/authz/bindings",
        params={"filter.status": "revoked"},
        cookies=authed_cookie,
    )
    assert resp.status_code == 422
    assert _forwarded_params(fake) == {"filter.status": "revoked"}
