"""Tests for the BFF attachment large-check pre-upload gate.

Covers GET /attachments/large-check:
* 401 unauth
* small file (below threshold) → allowed, is_large False, no lifecycle call
* large file + entitled → allowed True
* large file + NOT entitled → allowed False, reason set
* lifecycle error → FAIL-OPEN allowed True
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

# Threshold default is 50 MiB; pick sizes either side of it.
_THRESHOLD = 50 * 1024 * 1024
_SMALL = 1024
_LARGE = _THRESHOLD + 1


class FakeLifecycle:
    """Stub for the foundation lifecycle ``/large-attachment/resolve`` call."""

    def __init__(
        self,
        *,
        status: int = 200,
        allowed: bool = True,
        raise_connect: bool = False,
    ) -> None:
        self.status = status
        self.allowed = allowed
        self.raise_connect = raise_connect
        self.calls: list[tuple[str, str]] = []

    def route(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        self.calls.append((method, url))
        if self.raise_connect:
            raise httpx.ConnectError("boom", request=httpx.Request(method, url))
        resp = httpx.Response(
            self.status,
            json={
                "allowed": self.allowed,
                "tenant_enabled": True,
                "user_allowed": self.allowed,
            },
        )
        resp.request = httpx.Request(method, url)  # type: ignore[assignment]
        return resp


def _patch_async_client(
    fake: FakeLifecycle, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _AC:
        def __init__(self, *a: Any, **kw: Any) -> None:
            pass

        async def __aenter__(self) -> "_AC":
            return self

        async def __aexit__(self, *a: Any) -> None:
            pass

        async def get(self, url: str, **kw: Any) -> httpx.Response:
            return fake.route("GET", url, **kw)

    monkeypatch.setattr(httpx, "AsyncClient", _AC)


def test_large_check_requires_auth(client: TestClient) -> None:
    resp = client.get("/api/bff/v1/attachments/large-check?size_bytes=10")
    assert resp.status_code == 401


def test_small_file_allowed_without_lifecycle(
    client: TestClient,
    authed_cookie: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeLifecycle(allowed=False)  # would deny if consulted
    _patch_async_client(fake, monkeypatch)

    resp = client.get(
        f"/api/bff/v1/attachments/large-check?size_bytes={_SMALL}",
        cookies=authed_cookie,
    )
    assert resp.status_code == 200
    assert resp.json() == {"allowed": True, "is_large": False, "reason": None}
    # Small files must NOT consult lifecycle.
    assert fake.calls == []


def test_large_file_entitled_allowed(
    client: TestClient,
    authed_cookie: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeLifecycle(allowed=True)
    _patch_async_client(fake, monkeypatch)

    resp = client.get(
        f"/api/bff/v1/attachments/large-check?size_bytes={_LARGE}",
        cookies=authed_cookie,
    )
    assert resp.status_code == 200
    assert resp.json() == {"allowed": True, "is_large": True, "reason": None}
    assert any("large-attachment/resolve" in c[1] for c in fake.calls)


def test_large_file_not_entitled_denied(
    client: TestClient,
    authed_cookie: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeLifecycle(allowed=False)
    _patch_async_client(fake, monkeypatch)

    resp = client.get(
        f"/api/bff/v1/attachments/large-check?size_bytes={_LARGE}",
        cookies=authed_cookie,
    )
    assert resp.status_code == 200
    assert resp.json() == {
        "allowed": False,
        "is_large": True,
        "reason": "large_attachment_not_permitted",
    }


def test_lifecycle_error_fails_open(
    client: TestClient,
    authed_cookie: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeLifecycle(raise_connect=True)
    _patch_async_client(fake, monkeypatch)

    resp = client.get(
        f"/api/bff/v1/attachments/large-check?size_bytes={_LARGE}",
        cookies=authed_cookie,
    )
    assert resp.status_code == 200
    # FAIL-OPEN: lifecycle unreachable → allow (with is_large still True).
    assert resp.json() == {"allowed": True, "is_large": True, "reason": None}
