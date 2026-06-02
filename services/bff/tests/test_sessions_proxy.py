"""Tests for BFF → orchestrator session proxy endpoints."""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_list_sessions_requires_auth(client: TestClient) -> None:
    resp = client.get("/api/bff/v1/sessions")
    assert resp.status_code == 401


def test_list_sessions_forwards_bearer(
    client: TestClient, authed_cookie: dict[str, str], fake_foundation,
) -> None:
    resp = client.get("/api/bff/v1/sessions", cookies=authed_cookie)
    assert resp.status_code == 200
    # Confirm the BFF forwarded the request to orchestrator (not identity).
    # The fake's catch-all returns {"ok": True}; any orchestrator call
    # would land there if not specifically routed.
    orch_calls = [c for c in fake_foundation.calls if "orch-test" in c[1]]
    assert orch_calls, f"expected orchestrator call, got {fake_foundation.calls}"


def test_create_session_proxies_body(
    client: TestClient, authed_cookie: dict[str, str], fake_foundation,
) -> None:
    resp = client.post(
        "/api/bff/v1/sessions",
        cookies=authed_cookie,
        json={"title": "hello"},
    )
    assert resp.status_code in (200, 201)
    # Body actually forwarded
    posted_body = next(
        (c[2] for c in fake_foundation.calls if c[0] == "POST" and "orch-test" in c[1]),
        None,
    )
    assert posted_body == {"title": "hello"}
