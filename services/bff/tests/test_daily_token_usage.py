"""BFF proxy test for GET /runs/token-usage/daily."""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_daily_token_usage_requires_auth(client: TestClient) -> None:
    resp = client.get("/api/bff/v1/runs/token-usage/daily")
    assert resp.status_code == 401


def test_daily_token_usage_proxies_to_orchestrator(
    client: TestClient,
    authed_cookie: dict[str, str],
    fake_foundation,
) -> None:
    """BFF forwards the request to orchestrator and returns the response."""
    resp = client.get(
        "/api/bff/v1/runs/token-usage/daily",
        params={"days": 7},
        cookies=authed_cookie,
    )
    # The fake_foundation returns {"ok": True} for unknown URLs which the
    # BFF passes through as-is.
    assert resp.status_code == 200

    # Verify at least one call hit the orchestrator endpoint.
    orch_calls = [
        c for c in fake_foundation.calls
        if c[0] == "GET" and "orch-test" in c[1] and "token-usage/daily" in c[1]
    ]
    assert orch_calls, (
        f"expected orchestrator daily-token-usage call, got: {fake_foundation.calls}"
    )


def test_daily_token_usage_default_days_param(
    client: TestClient,
    authed_cookie: dict[str, str],
    fake_foundation,
) -> None:
    """Endpoint is reachable with the default days param (no explicit value)."""
    resp = client.get(
        "/api/bff/v1/runs/token-usage/daily",
        cookies=authed_cookie,
    )
    assert resp.status_code == 200

    orch_calls = [
        c for c in fake_foundation.calls
        if c[0] == "GET" and "orch-test" in c[1] and "token-usage/daily" in c[1]
    ]
    assert orch_calls, "expected orchestrator daily-token-usage call"
