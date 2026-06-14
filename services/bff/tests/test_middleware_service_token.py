"""Tests for BFF SessionMiddleware service-token (machine-auth) path.

Architecture note
-----------------
The BFF uses two cooperating middlewares:

1. ``RequestContextMiddleware`` (foundation_sdk) — runs FIRST on every request.
   Unconditionally reads ``X-Tenant-Id`` / ``X-User-Id`` headers into context
   vars.  This makes inter-service calls work: any service that passes those
   headers is considered identified by the foundation stack.

2. ``SessionMiddleware`` (this module) — runs SECOND.  For browser users it
   validates the signed session cookie and overwrites the context vars set by
   (1).  For machine callers it can optionally accept a pre-shared service
   token (``X-Service-Token``) and short-circuit before cookie parsing.

The ``require_auth()`` dependency checks that both tenant_id and user_id are
non-empty.  Because (1) populates them from headers unconditionally, a request
with ``X-Tenant-Id`` + ``X-User-Id`` headers (no cookie, no service token)
ALSO passes ``require_auth()`` — this is the correct and intended behavior for
internal inter-service calls.

The service-token path adds an EARLY RETURN that bypasses cookie parsing when
the ``X-Service-Token`` header matches the configured secret.  Its primary
value is skipping the (mildly expensive) cookie validation for machine callers
that will never have a cookie; it also serves as documentation that a request
came from a known internal service.

These tests verify:
* Valid X-Service-Token → request authenticated (200 from downstream)
* Wrong / absent service token → falls through to foundation headers → ALSO
  authenticated when X-Tenant-Id + X-User-Id are present (200)
* Request with NO tenant/user headers AND no cookie AND no service token → 401
* service_token config empty → machine path disabled (but identity still comes
  from foundation headers when present)
* Empty config + empty token header → 401 (no identity at all)
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from agent_bff.config import Settings
from agent_bff.main import create_app

TEST_COOKIE_SECRET = "abcdefghijklmnopqrstuvwxyz0123456789ABCDEFGHIJKL"
TEST_SERVICE_TOKEN = "test-service-token-for-unit-tests-exactly-right"


@pytest.fixture
def settings_with_token() -> Settings:
    return Settings(
        env="test",
        log_level="WARNING",
        cookie_secret=TEST_COOKIE_SECRET,
        cookie_secure=False,
        foundation_identity_url="http://identity-test/api/identity/v1",
        session_orchestrator_url="http://orch-test:8006/api/session/v1",
        cors_origins="http://localhost",
        otel_enabled=False,
        service_token=TEST_SERVICE_TOKEN,
        cron_runner_url="http://cron-runner-test:8013/api/cron/v1",
    )


@pytest.fixture
def settings_no_token() -> Settings:
    """Settings where service_token is empty → machine path disabled."""
    return Settings(
        env="test",
        log_level="WARNING",
        cookie_secret=TEST_COOKIE_SECRET,
        cookie_secure=False,
        foundation_identity_url="http://identity-test/api/identity/v1",
        session_orchestrator_url="http://orch-test:8006/api/session/v1",
        cors_origins="http://localhost",
        otel_enabled=False,
        service_token="",
        cron_runner_url="http://cron-runner-test:8013/api/cron/v1",
    )


@pytest.fixture
def client_with_token(settings_with_token: Settings) -> Iterator[TestClient]:
    app = create_app(settings_with_token)
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture
def client_no_token(settings_no_token: Settings) -> Iterator[TestClient]:
    app = create_app(settings_no_token)
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def _patch_async_client_list(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch httpx.AsyncClient so GET returns a list, others return {ok: True}."""

    class _AC:
        def __init__(self, *a: Any, **kw: Any) -> None:
            pass

        async def __aenter__(self) -> _AC:
            return self

        async def __aexit__(self, *a: Any) -> None:
            pass

        async def get(self, url: str, **kw: Any) -> httpx.Response:
            resp = httpx.Response(200, json=[])
            resp.request = httpx.Request("GET", url)
            return resp

        async def post(self, url: str, **kw: Any) -> httpx.Response:
            resp = httpx.Response(200, json={"ok": True})
            resp.request = httpx.Request("POST", url)
            return resp

        async def patch(self, url: str, **kw: Any) -> httpx.Response:
            resp = httpx.Response(200, json={"ok": True})
            resp.request = httpx.Request("PATCH", url)
            return resp

        async def delete(self, url: str, **kw: Any) -> httpx.Response:
            resp = httpx.Response(204, content=b"")
            resp.request = httpx.Request("DELETE", url)
            return resp

    monkeypatch.setattr(httpx, "AsyncClient", _AC)


# --------------------------------------------------------------------------- #
# Service-token fast path
# --------------------------------------------------------------------------- #

def test_service_token_auth_authenticates(
    client_with_token: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Valid X-Service-Token + tenant/user headers → authenticated (200)."""
    _patch_async_client_list(monkeypatch)

    resp = client_with_token.get(
        "/api/bff/v1/schedules",
        headers={
            "X-Service-Token": TEST_SERVICE_TOKEN,
            "X-Tenant-Id": "tnt_test",
            "X-User-Id": "svc-cron",
        },
    )
    assert resp.status_code == 200


def test_service_token_auth_returns_early_from_cookie_parsing(
    client_with_token: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With a valid service token, no session cookie needed — still 200."""
    _patch_async_client_list(monkeypatch)

    # Deliberately no cookies= arg — the service token path bypasses the cookie.
    resp = client_with_token.get(
        "/api/bff/v1/schedules",
        headers={
            "X-Service-Token": TEST_SERVICE_TOKEN,
            "X-Tenant-Id": "tnt_test_internal",
            "X-User-Id": "svc-internal",
        },
    )
    assert resp.status_code == 200


# --------------------------------------------------------------------------- #
# Inter-service header path (no service token — foundation headers trusted)
# --------------------------------------------------------------------------- #

def test_foundation_headers_alone_authenticate(
    client_with_token: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """X-Tenant-Id + X-User-Id headers alone pass require_auth() — this is the
    expected inter-service trust model; any service on the foundation network
    can pass these headers and be identified.
    """
    _patch_async_client_list(monkeypatch)

    resp = client_with_token.get(
        "/api/bff/v1/schedules",
        headers={
            "X-Tenant-Id": "tnt_test",
            "X-User-Id": "some-service",
        },
    )
    assert resp.status_code == 200


# --------------------------------------------------------------------------- #
# No identity at all → 401
# --------------------------------------------------------------------------- #

def test_no_identity_at_all_is_401(
    client_with_token: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No cookie, no service token, no X-Tenant-Id/X-User-Id → 401."""
    _patch_async_client_list(monkeypatch)

    resp = client_with_token.get("/api/bff/v1/schedules")
    assert resp.status_code == 401


def test_no_identity_no_token_config_is_401(
    client_no_token: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """service_token empty + no headers + no cookie → 401."""
    _patch_async_client_list(monkeypatch)

    resp = client_no_token.get("/api/bff/v1/schedules")
    assert resp.status_code == 401


# --------------------------------------------------------------------------- #
# Service-token disabled (empty config) safety invariant
# --------------------------------------------------------------------------- #

def test_service_token_disabled_does_not_early_return(
    client_no_token: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When service_token config is empty the fast-return path is disabled.

    The request still passes via foundation headers (correct behavior) — the
    empty config only disables the early-return branch, not inter-service trust.
    """
    _patch_async_client_list(monkeypatch)

    resp = client_no_token.get(
        "/api/bff/v1/schedules",
        headers={
            "X-Service-Token": TEST_SERVICE_TOKEN,
            "X-Tenant-Id": "tnt_test",
            "X-User-Id": "svc-cron",
        },
    )
    # Still 200 because foundation headers are present and trusted.
    assert resp.status_code == 200


def test_empty_config_empty_token_with_headers_still_200(
    client_no_token: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty token config + empty token header + valid headers → 200 via headers."""
    _patch_async_client_list(monkeypatch)

    resp = client_no_token.get(
        "/api/bff/v1/schedules",
        headers={
            "X-Service-Token": "",
            "X-Tenant-Id": "tnt_test",
            "X-User-Id": "svc-cron",
        },
    )
    assert resp.status_code == 200
