"""Unit tests for the session-orchestrator RBAC guard (authz_guard.py).

Covers all four Fail-closed deny points:
  1. user_id missing → 403
  2. user_id == "SYSTEM" → 403
  3. user_id format invalid → 403
  4. authz timeout / network error → 403
  5. authz returns non-200 (e.g. 422) → 403
  6. authz allowed=false → 403
  7. authz allowed=true → guard passes (200 from business logic)

Uses the ``app_client`` fixture from conftest.py (DB mocked out).
``httpx.AsyncClient`` is patched per-test to control authz responses.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from tests._helpers import TEST_TENANT, TEST_USER, make_scalar_result

# The authz guard is imported via the guard module itself.
_GUARD_MODULE = "agent_session_orchestrator.api.authz_guard.httpx.AsyncClient"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_authz_resp(allowed: bool, status_code: int = 200) -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.json = MagicMock(return_value={"allowed": allowed, "decision_path": "test"})
    return resp


def _mock_authz_client(response: MagicMock) -> AsyncMock:
    """Return an AsyncClient context-manager mock that returns ``response`` for any POST."""
    client_instance = AsyncMock()
    client_instance.post = AsyncMock(return_value=response)
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=client_instance)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx, client_instance


def _mock_authz_error(exc: Exception) -> AsyncMock:
    """Return an AsyncClient context-manager mock that raises ``exc`` on POST."""
    client_instance = AsyncMock()
    client_instance.post = AsyncMock(side_effect=exc)
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=client_instance)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx, client_instance


# ---------------------------------------------------------------------------
# Deny point 1: user_id absent / SYSTEM / malformed
# ---------------------------------------------------------------------------

class TestDenyPoint1:
    """Guard returns 403 before making any authz network call."""

    def test_no_user_id_header(self, app_client: TestClient) -> None:
        with patch(_GUARD_MODULE) as mock_cls:
            resp = app_client.get(
                "/api/session/v1/sessions",
                headers={"X-Tenant-Id": TEST_TENANT},
            )
        assert resp.status_code == 403
        mock_cls.assert_not_called()

    def test_user_id_system(self, app_client: TestClient) -> None:
        with patch(_GUARD_MODULE) as mock_cls:
            resp = app_client.get(
                "/api/session/v1/sessions",
                headers={"X-Tenant-Id": TEST_TENANT, "X-User-Id": "SYSTEM"},
            )
        assert resp.status_code == 403
        mock_cls.assert_not_called()

    def test_user_id_wrong_format(self, app_client: TestClient) -> None:
        with patch(_GUARD_MODULE) as mock_cls:
            resp = app_client.get(
                "/api/session/v1/sessions",
                headers={"X-Tenant-Id": TEST_TENANT, "X-User-Id": "bad-format"},
            )
        assert resp.status_code == 403
        mock_cls.assert_not_called()

    def test_tenant_id_missing(self, app_client: TestClient) -> None:
        """Missing tenant_id → guard denies, no authz call."""
        with patch(_GUARD_MODULE) as mock_cls:
            resp = app_client.get(
                "/api/session/v1/sessions",
                headers={"X-User-Id": TEST_USER},
            )
        # Either 401 (from current_tenant dep) or 403 (from guard).
        # Guard's tenant_id check runs before network; either status means
        # the request was rejected before reaching business logic.
        assert resp.status_code in (401, 403)
        mock_cls.assert_not_called()


# ---------------------------------------------------------------------------
# Deny point 2: authz unreachable
# ---------------------------------------------------------------------------

class TestDenyPoint2Unreachable:
    def test_timeout_returns_403(self, app_client: TestClient) -> None:
        ctx, _ = _mock_authz_error(httpx.TimeoutException("timed out"))
        with patch(_GUARD_MODULE, return_value=ctx):
            resp = app_client.get(
                "/api/session/v1/sessions",
                headers={"X-Tenant-Id": TEST_TENANT, "X-User-Id": TEST_USER},
            )
        assert resp.status_code == 403
        assert "unreachable" in resp.json().get("detail", "").lower()

    def test_connect_error_returns_403(self, app_client: TestClient) -> None:
        ctx, _ = _mock_authz_error(httpx.ConnectError("connection refused"))
        with patch(_GUARD_MODULE, return_value=ctx):
            resp = app_client.get(
                "/api/session/v1/sessions",
                headers={"X-Tenant-Id": TEST_TENANT, "X-User-Id": TEST_USER},
            )
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Deny point 3: authz non-200 response
# ---------------------------------------------------------------------------

class TestDenyPoint3NonOk:
    @pytest.mark.parametrize("bad_status", [422, 503, 500, 401])
    def test_non_200_returns_403(
        self, app_client: TestClient, bad_status: int,
    ) -> None:
        ctx, _ = _mock_authz_client(_make_authz_resp(allowed=True, status_code=bad_status))
        with patch(_GUARD_MODULE, return_value=ctx):
            resp = app_client.get(
                "/api/session/v1/sessions",
                headers={"X-Tenant-Id": TEST_TENANT, "X-User-Id": TEST_USER},
            )
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Deny point 4: allowed != True
# ---------------------------------------------------------------------------

class TestDenyPoint4AllowedFalse:
    def test_allowed_false_returns_403(self, app_client: TestClient) -> None:
        ctx, _ = _mock_authz_client(_make_authz_resp(allowed=False, status_code=200))
        with patch(_GUARD_MODULE, return_value=ctx):
            resp = app_client.get(
                "/api/session/v1/sessions",
                headers={"X-Tenant-Id": TEST_TENANT, "X-User-Id": TEST_USER},
            )
        assert resp.status_code == 403
        assert "denied" in resp.json().get("detail", "").lower()


# ---------------------------------------------------------------------------
# Happy path: allowed=True → guard passes, handler returns 200
# ---------------------------------------------------------------------------

class TestAllowedPassThrough:
    def test_allowed_true_reaches_handler(
        self, app_client: TestClient, mock_db: AsyncMock,
    ) -> None:
        mock_db.execute.return_value = make_scalar_result([])

        ctx, _ = _mock_authz_client(_make_authz_resp(allowed=True, status_code=200))
        with patch(_GUARD_MODULE, return_value=ctx):
            resp = app_client.get(
                "/api/session/v1/sessions",
                headers={"X-Tenant-Id": TEST_TENANT, "X-User-Id": TEST_USER},
            )
        assert resp.status_code == 200
        assert "items" in resp.json()

    def test_authz_receives_correct_payload(
        self, app_client: TestClient, mock_db: AsyncMock,
    ) -> None:
        """Guard must send user_id, action, resource_type, and X-Tenant-Id."""
        mock_db.execute.return_value = make_scalar_result([])

        ctx, client_instance = _mock_authz_client(
            _make_authz_resp(allowed=True, status_code=200)
        )
        with patch(_GUARD_MODULE, return_value=ctx):
            resp = app_client.get(
                "/api/session/v1/sessions",
                headers={"X-Tenant-Id": TEST_TENANT, "X-User-Id": TEST_USER},
            )
        assert resp.status_code == 200

        call_kwargs = client_instance.post.call_args
        assert call_kwargs is not None

        url = call_kwargs.args[0] if call_kwargs.args else ""
        assert "/check" in url

        headers_sent = call_kwargs.kwargs.get("headers", {})
        assert headers_sent.get("X-Tenant-Id") == TEST_TENANT

        body_sent = call_kwargs.kwargs.get("json", {})
        assert body_sent["user_id"] == TEST_USER
        assert body_sent["action"] == "session.read"
        assert body_sent["resource_type"] == "session"


# ---------------------------------------------------------------------------
# Health endpoint: exempt from RBAC (no guard wired)
# ---------------------------------------------------------------------------

class TestHealthExempt:
    def test_healthz_no_guard(self, app_client: TestClient) -> None:
        """Health endpoint must succeed without any headers."""
        with patch(_GUARD_MODULE) as mock_cls:
            resp = app_client.get("/api/session/v1/healthz")
        assert resp.status_code == 200
        mock_cls.assert_not_called()
