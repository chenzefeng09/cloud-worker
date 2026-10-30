"""Shared fakes for the server-side web-session tests.

* :class:`FakeIdentity` — an in-process foundation identity (plus authz
  ``/my-permissions`` and the orchestrator's session SSE stream) behind
  ``httpx.MockTransport``: login, refresh (with rotation, scripted failures
  and an optional gate to hold a refresh in flight), revoke, ``/sessions/me``
  and ``/sessions``.
* :func:`sqlite_session_factory` — a file-backed SQLite database holding the
  ``web_sessions`` table, installed as the BFF's DB.  ``NullPool`` opens each
  connection in the running event loop, so it works from TestClient's loop as
  well as pytest-asyncio's — and tests never reach the default Postgres DSN.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from jose import jwt
from sqlalchemy import create_engine, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from agent_bff.domain.db import set_session_factory
from agent_bff.domain.web_sessions import WebSession

# Captured at import, before any test patches httpx.AsyncClient.
REAL_ASYNC_CLIENT = httpx.AsyncClient

TENANT = "tnt_01J5TEST00000000000000TNT1"
USER = "usr_01J5TEST0000000000000USER1"


def make_token(kind: str, *, ttl: int, serial: int = 0, clearance: str = "L3") -> str:
    """A JWT-shaped token.  The BFF only reads its claims, never verifies it."""
    return jwt.encode(
        {
            "exp": int(time.time()) + ttl,
            "jti": f"{kind}-{serial}",
            "clearance": clearance,
            "sub": "kc-sub",
        },
        "test-only-signing-key",
        algorithm="HS256",
    )


def sqlite_session_factory(path: Path) -> async_sessionmaker[AsyncSession]:
    sync_engine = create_engine(f"sqlite:///{path}")
    WebSession.__table__.create(sync_engine, checkfirst=True)  # type: ignore[attr-defined]
    sync_engine.dispose()
    factory = async_sessionmaker(
        create_async_engine(f"sqlite+aiosqlite:///{path}", poolclass=NullPool),
        class_=AsyncSession,
        expire_on_commit=False,
    )
    set_session_factory(factory)
    return factory


async def all_rows(factory: async_sessionmaker[AsyncSession]) -> list[WebSession]:
    async with factory() as session:
        return list((await session.execute(select(WebSession))).scalars().all())


class FakeIdentity:
    """Stand-in for foundation identity; see the module docstring."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.access_ttl = 300
        self.login_status = 201
        self.login_body: dict[str, Any] | None = None
        self.refresh_calls = 0
        self.refresh_bodies: list[dict[str, Any]] = []
        # Called with the 1-based refresh call number; None → rotate normally.
        self.refresh_response: Callable[[int], httpx.Response] | None = None
        # When set, a refresh waits for it before answering.
        self.refresh_gate: asyncio.Event | None = None
        self.revoked: list[httpx.Request] = []
        self.issued: list[dict[str, Any]] = []

    def next_bundle(self) -> dict[str, Any]:
        serial = len(self.issued) + 1
        now = datetime.now(UTC)
        bundle = {
            "id": f"sess_{serial}",
            "user_id": USER,
            "tenant_id": TENANT,
            "access_token": make_token("access", ttl=self.access_ttl, serial=serial),
            "refresh_token": make_token("refresh", ttl=28_800, serial=serial),
            "expires_at": (now + timedelta(seconds=self.access_ttl)).isoformat(),
            "issued_at": now.isoformat(),
            "scopes": ["openid"],
        }
        self.issued.append(bundle)
        return bundle

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path, method = request.url.path, request.method
        if method == "POST" and path.endswith("/sessions/current/refresh"):
            self.refresh_calls += 1
            self.refresh_bodies.append(json.loads(request.content))
            if self.refresh_gate is not None:
                await self.refresh_gate.wait()
            if self.refresh_response is not None:
                return self.refresh_response(self.refresh_calls)
            return httpx.Response(200, json=self.next_bundle())
        if method == "POST" and path.endswith("/sessions"):
            if self.login_status != 201:
                return httpx.Response(self.login_status, json=self.login_body or {})
            return httpx.Response(201, json=self.login_body or self.next_bundle())
        if method == "GET" and path.endswith("/stream"):
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=b"event: ping\ndata: {}\n\n",
            )
        if method == "DELETE" and path.endswith("/sessions/current"):
            self.revoked.append(request)
            return httpx.Response(204)
        if method == "GET" and path.endswith("/sessions/me"):
            return httpx.Response(
                200,
                json={"user_id": USER, "tenant_id": TENANT, "email": "u@example.com"},
            )
        if method == "GET" and path.endswith("/sessions"):
            return httpx.Response(200, json=[])
        if path.endswith("/my-permissions"):
            return httpx.Response(200, json={"permissions": []})
        return httpx.Response(404, json={"detail": "not faked"})

    def bearers(self, method: str, suffix: str) -> list[str]:
        """``Authorization`` headers identity received on matching requests."""
        return [
            r.headers.get("authorization", "")
            for r in self.requests
            if r.method == method and r.url.path.endswith(suffix)
        ]

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        transport = httpx.MockTransport(self)

        def _client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
            kwargs["transport"] = transport
            return REAL_ASYNC_CLIENT(*args, **kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", _client)
