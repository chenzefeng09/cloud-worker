"""Tests for BFF schedules proxy endpoints.

Covers:
* GET    /schedules        — 401 unauth, 200 auth + proxied to cron-runner
* POST   /schedules        — 401 unauth, 201 auth + proxied
* PATCH  /schedules/{id}   — 401 unauth, 200 auth + proxied
* DELETE /schedules/{id}   — 401 unauth, 204 auth + proxied
* Upstream 4xx surfaced as HTTPException (_raise_for_upstream)
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Fake upstream for cron-runner
# ---------------------------------------------------------------------------

class FakeCronRunnerUpstream:
    """Configurable stub for cron-runner HTTP API."""

    def __init__(self, *, status: int = 200, body: dict | list | None = None) -> None:
        self.status = status
        self.body: Any = body if body is not None else []
        self.calls: list[tuple[str, str]] = []

    def route(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        self.calls.append((method, url))
        resp = httpx.Response(self.status, json=self.body)
        resp.request = httpx.Request(method, url)
        return resp


def _patch_async_client(fake: FakeCronRunnerUpstream, monkeypatch: pytest.MonkeyPatch) -> None:
    class _AC:
        def __init__(self, *a: Any, **kw: Any) -> None:
            pass

        async def __aenter__(self) -> _AC:
            return self

        async def __aexit__(self, *a: Any) -> None:
            pass

        async def get(self, url: str, **kw: Any) -> httpx.Response:
            return fake.route("GET", url, **kw)

        async def post(self, url: str, **kw: Any) -> httpx.Response:
            return fake.route("POST", url, **kw)

        async def patch(self, url: str, **kw: Any) -> httpx.Response:
            return fake.route("PATCH", url, **kw)

        async def delete(self, url: str, **kw: Any) -> httpx.Response:
            return fake.route("DELETE", url, **kw)

    monkeypatch.setattr(httpx, "AsyncClient", _AC)


# ---------------------------------------------------------------------------
# GET /schedules
# ---------------------------------------------------------------------------

def test_list_schedules_requires_auth(client: TestClient) -> None:
    resp = client.get("/api/bff/v1/schedules")
    assert resp.status_code == 401


def test_list_schedules_proxied(
    client: TestClient,
    authed_cookie: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeCronRunnerUpstream(
        body=[{"id": "cron_ABC", "name": "Daily Report", "cron_expression": "0 8 * * *"}]
    )
    _patch_async_client(fake, monkeypatch)

    resp = client.get("/api/bff/v1/schedules", cookies=authed_cookie)
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert data[0]["id"] == "cron_ABC"
    assert any("cron-jobs" in c[1] for c in fake.calls)


# ---------------------------------------------------------------------------
# POST /schedules
# ---------------------------------------------------------------------------

def test_create_schedule_requires_auth(client: TestClient) -> None:
    resp = client.post(
        "/api/bff/v1/schedules",
        json={"app_id": "app_01", "name": "test", "cron_expression": "0 * * * *"},
    )
    assert resp.status_code == 401


def test_create_schedule_proxied(
    client: TestClient,
    authed_cookie: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeCronRunnerUpstream(
        status=201,
        body={"id": "cron_NEW", "name": "Daily", "cron_expression": "0 8 * * *"},
    )
    _patch_async_client(fake, monkeypatch)

    resp = client.post(
        "/api/bff/v1/schedules",
        cookies=authed_cookie,
        json={"app_id": "app_01", "name": "Daily", "cron_expression": "0 8 * * *"},
    )
    assert resp.status_code == 201
    assert resp.json()["id"] == "cron_NEW"
    assert any("cron-jobs" in c[1] and c[0] == "POST" for c in fake.calls)


def test_create_schedule_surfaces_upstream_400(
    client: TestClient,
    authed_cookie: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Upstream 400 (invalid cron expression) is surfaced as 400."""
    fake = FakeCronRunnerUpstream(
        status=400, body={"detail": "Invalid cron expression 'bad'"}
    )
    _patch_async_client(fake, monkeypatch)

    resp = client.post(
        "/api/bff/v1/schedules",
        cookies=authed_cookie,
        json={"app_id": "app_01", "name": "Bad", "cron_expression": "bad"},
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# PATCH /schedules/{id}
# ---------------------------------------------------------------------------

def test_update_schedule_requires_auth(client: TestClient) -> None:
    resp = client.patch(
        "/api/bff/v1/schedules/cron_ABC",
        json={"enabled": False},
    )
    assert resp.status_code == 401


def test_update_schedule_proxied(
    client: TestClient,
    authed_cookie: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeCronRunnerUpstream(
        body={"id": "cron_ABC", "enabled": False, "cron_expression": "0 8 * * *"}
    )
    _patch_async_client(fake, monkeypatch)

    resp = client.patch(
        "/api/bff/v1/schedules/cron_ABC",
        cookies=authed_cookie,
        json={"enabled": False},
    )
    assert resp.status_code == 200
    assert resp.json()["enabled"] is False
    assert any("cron_ABC" in c[1] and c[0] == "PATCH" for c in fake.calls)


def test_update_schedule_surfaces_upstream_404(
    client: TestClient,
    authed_cookie: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeCronRunnerUpstream(status=404, body={"detail": "CronJob not found"})
    _patch_async_client(fake, monkeypatch)

    resp = client.patch(
        "/api/bff/v1/schedules/cron_MISSING",
        cookies=authed_cookie,
        json={"enabled": True},
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# DELETE /schedules/{id}
# ---------------------------------------------------------------------------

def test_delete_schedule_requires_auth(client: TestClient) -> None:
    resp = client.delete("/api/bff/v1/schedules/cron_ABC")
    assert resp.status_code == 401


def test_delete_schedule_proxied(
    client: TestClient,
    authed_cookie: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeCronRunnerUpstream(status=204, body=None)
    _patch_async_client(fake, monkeypatch)

    resp = client.delete(
        "/api/bff/v1/schedules/cron_ABC",
        cookies=authed_cookie,
    )
    assert resp.status_code == 204
    assert any("cron_ABC" in c[1] and c[0] == "DELETE" for c in fake.calls)


def test_delete_schedule_surfaces_upstream_404(
    client: TestClient,
    authed_cookie: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeCronRunnerUpstream(status=404, body={"detail": "not found"})
    _patch_async_client(fake, monkeypatch)

    resp = client.delete(
        "/api/bff/v1/schedules/cron_MISSING",
        cookies=authed_cookie,
    )
    assert resp.status_code == 404
