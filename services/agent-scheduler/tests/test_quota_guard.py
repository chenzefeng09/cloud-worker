"""Unit tests for agent-scheduler QuotaGuard.

Verifies fail-closed behaviour:
  1. billing unreachable → HTTP 503
  2. billing returns non-200 → HTTP 502
  3. billing returns allowed=false → HTTP 402
  4. billing returns allowed=true → guard passes, request proceeds
  5. record failure is best-effort (doesn't raise)
"""

from __future__ import annotations

import httpx
import pytest

from agent_scheduler.services.quota_guard import QuotaGuard

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _FakeBillingClient:
    """Minimal async context-manager httpx.AsyncClient stand-in."""

    def __init__(self, *, status: int, body: dict | None = None, raise_exc: Exception | None = None):
        self._status = status
        self._body = body or {}
        self._raise_exc = raise_exc
        self.calls: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    async def post(self, url: str, **kwargs) -> httpx.Response:
        self.calls.append({"url": url, **kwargs})
        if self._raise_exc is not None:
            raise self._raise_exc
        return httpx.Response(self._status, json=self._body)


# ---------------------------------------------------------------------------
# check() — fail-closed
# ---------------------------------------------------------------------------

class TestQuotaGuardCheck:

    @pytest.mark.asyncio
    async def test_billing_unreachable_raises_503(self, monkeypatch):
        """If billing cannot be reached, check() must raise HTTP 503."""
        guard = QuotaGuard(billing_url="http://billing:8007/api/billing/v1")
        fake = _FakeBillingClient(status=0, raise_exc=httpx.ConnectError("refused"))
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: fake)

        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc_info:
            await guard.check(tenant_id="tnt_TEST", dimension="agent_sessions", requested=1)

        assert exc_info.value.status_code == 503

    @pytest.mark.asyncio
    async def test_billing_non_200_raises_502(self, monkeypatch):
        """Non-200 response from billing → HTTP 502."""
        guard = QuotaGuard(billing_url="http://billing:8007/api/billing/v1")
        fake = _FakeBillingClient(status=503, body={"error": "billing down"})
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: fake)

        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc_info:
            await guard.check(tenant_id="tnt_TEST", dimension="agent_sessions", requested=1)

        assert exc_info.value.status_code == 502

    @pytest.mark.asyncio
    async def test_allowed_false_raises_402(self, monkeypatch):
        """allowed=false in billing response → HTTP 402."""
        guard = QuotaGuard(billing_url="http://billing:8007/api/billing/v1")
        fake = _FakeBillingClient(
            status=200,
            body={"allowed": False, "dimension": "agent_sessions", "current": 10, "limit": 10, "remaining": 0},
        )
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: fake)

        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc_info:
            await guard.check(tenant_id="tnt_TEST", dimension="agent_sessions", requested=1)

        assert exc_info.value.status_code == 402
        detail = exc_info.value.detail
        assert detail["message"] == "quota exceeded"
        assert detail["remaining"] == 0

    @pytest.mark.asyncio
    async def test_allowed_true_passes(self, monkeypatch):
        """allowed=true → no exception raised."""
        guard = QuotaGuard(billing_url="http://billing:8007/api/billing/v1")
        fake = _FakeBillingClient(
            status=200,
            body={"allowed": True, "dimension": "agent_sessions", "current": 3, "limit": 10, "remaining": 7},
        )
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: fake)

        # Should not raise
        await guard.check(tenant_id="tnt_TEST", dimension="agent_sessions", requested=1)

    @pytest.mark.asyncio
    async def test_check_sends_correct_payload(self, monkeypatch):
        """check() must send dimension, requested, and X-Tenant-Id header."""
        guard = QuotaGuard(billing_url="http://billing:8007/api/billing/v1")
        fake = _FakeBillingClient(
            status=200,
            body={"allowed": True, "dimension": "agent_sessions"},
        )
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: fake)

        await guard.check(tenant_id="tnt_MYTEST", dimension="agent_sessions", requested=2)

        assert len(fake.calls) == 1
        call = fake.calls[0]
        assert call["json"] == {"dimension": "agent_sessions", "requested": 2}
        assert call["headers"]["X-Tenant-Id"] == "tnt_MYTEST"
        assert "/quota/check" in call["url"]


# ---------------------------------------------------------------------------
# record() — best-effort, never raises
# ---------------------------------------------------------------------------

class TestQuotaGuardRecord:

    @pytest.mark.asyncio
    async def test_record_does_not_raise_on_network_error(self, monkeypatch):
        """record() must swallow network errors (best-effort)."""
        guard = QuotaGuard(billing_url="http://billing:8007/api/billing/v1")
        fake = _FakeBillingClient(status=0, raise_exc=httpx.ConnectError("refused"))
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: fake)

        # Should not raise
        await guard.record(tenant_id="tnt_TEST", dimension="agent_sessions", amount=1.0)

    @pytest.mark.asyncio
    async def test_record_does_not_raise_on_non_200(self, monkeypatch):
        """record() logs and continues on non-200 billing responses."""
        guard = QuotaGuard(billing_url="http://billing:8007/api/billing/v1")
        fake = _FakeBillingClient(status=500, body={"error": "internal"})
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: fake)

        # Should not raise
        await guard.record(tenant_id="tnt_TEST", dimension="agent_sessions", amount=1.0)

    @pytest.mark.asyncio
    async def test_record_sends_correct_payload(self, monkeypatch):
        """record() must POST dimension, amount, description and X-Tenant-Id."""
        guard = QuotaGuard(billing_url="http://billing:8007/api/billing/v1")
        fake = _FakeBillingClient(status=201, body={})
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: fake)

        await guard.record(
            tenant_id="tnt_MYTEST",
            dimension="agent_sessions",
            amount=1.0,
            description="spawn run run_ABC",
        )

        assert len(fake.calls) == 1
        call = fake.calls[0]
        assert call["json"]["dimension"] == "agent_sessions"
        assert call["json"]["amount"] == 1.0
        assert call["json"]["description"] == "spawn run run_ABC"
        assert call["headers"]["X-Tenant-Id"] == "tnt_MYTEST"
        assert "/quota/usage" in call["url"]
