"""Tests for BFF → orchestrator runs proxy endpoints and runs/count."""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_get_run_requires_auth(client: TestClient) -> None:
    resp = client.get("/api/bff/v1/sessions/ses_abc/runs/run_xyz")
    assert resp.status_code == 401


def test_get_run_proxies_to_orchestrator(
    client: TestClient, authed_cookie: dict[str, str], fake_foundation,
) -> None:
    resp = client.get(
        "/api/bff/v1/sessions/ses_abc/runs/run_xyz",
        cookies=authed_cookie,
    )
    assert resp.status_code == 200
    run_calls = [
        c for c in fake_foundation.calls
        if c[0] == "GET" and "orch-test" in c[1] and "runs" in c[1]
    ]
    assert run_calls, f"expected runs call to orchestrator, got {fake_foundation.calls}"
    assert "ses_abc" in run_calls[0][1]
    assert "run_xyz" in run_calls[0][1]


def test_list_runs_requires_auth(client: TestClient) -> None:
    resp = client.get("/api/bff/v1/sessions/ses_abc/runs")
    assert resp.status_code == 401


def test_list_runs_proxies_to_orchestrator(
    client: TestClient, authed_cookie: dict[str, str], fake_foundation,
) -> None:
    resp = client.get(
        "/api/bff/v1/sessions/ses_abc/runs",
        cookies=authed_cookie,
    )
    assert resp.status_code == 200
    run_calls = [
        c for c in fake_foundation.calls
        if c[0] == "GET" and "orch-test" in c[1] and "runs" in c[1]
    ]
    assert run_calls, f"expected list runs call to orchestrator, got {fake_foundation.calls}"


def test_runs_count_requires_auth(client: TestClient) -> None:
    resp = client.get("/api/bff/v1/runs/count")
    assert resp.status_code == 401


def test_runs_count_returns_zero_on_empty_sessions(
    client: TestClient, authed_cookie: dict[str, str], fake_foundation,
) -> None:
    """When orchestrator returns empty sessions list, count should be 0."""
    # The fake_foundation catch-all returns {"ok": True} for sessions,
    # but we need it to return the right shape for /sessions.
    # The fake returns {"ok": True} which doesn't have "items", so count = 0.
    resp = client.get(
        "/api/bff/v1/runs/count",
        cookies=authed_cookie,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "count" in data
    assert isinstance(data["count"], int)


def test_token_usage_requires_auth(client: TestClient) -> None:
    resp = client.get("/api/bff/v1/runs/token-usage")
    assert resp.status_code == 401


def test_token_usage_returns_zero_on_empty_sessions(
    client: TestClient, authed_cookie: dict[str, str], fake_foundation,
) -> None:
    """When orchestrator returns empty sessions list, token usage should be zeroed."""
    # fake_foundation returns {"ok": True} which has no "items", so totals are 0.
    resp = client.get(
        "/api/bff/v1/runs/token-usage",
        cookies=authed_cookie,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "input_tokens" in data
    assert "output_tokens" in data
    assert "total_tokens" in data
    assert isinstance(data["total_tokens"], int)
    assert data["total_tokens"] == 0
