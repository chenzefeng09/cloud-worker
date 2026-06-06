"""Tests: GET/PUT /notifications/preferences.

Covers:
1. Unauthenticated → 401/403
2. GET with no DB rows → all defaults returned (6 events)
3. GET after partial PUT → updated values + defaults for untouched events
4. PUT with full payload → all 6 events stored, re-GET reflects them
5. PUT with partial payload → only listed events updated, others keep defaults
6. PUT with unknown event_key → silently ignored (no 400)
7. Two-user isolation: user A's prefs don't leak to user B
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from agent_bff.config import Settings
from agent_bff.domain import db as db_module
from agent_bff.domain.models import Base
from agent_bff.main import create_app

TEST_COOKIE_SECRET = "abcdefghijklmnopqrstuvwxyz0123456789ABCDEFGHIJKL"

USER_A = "usr_01J5TEST0000000000000USER1"
USER_B = "usr_01J5TEST0000000000000USER2"
TENANT = "tnt_01J5TEST00000000000000TNT1"


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def settings() -> Settings:
    return Settings(
        env="test",
        log_level="WARNING",
        cookie_secret=TEST_COOKIE_SECRET,
        cookie_secure=False,
        db_dsn="sqlite+aiosqlite:///:memory:",
        foundation_identity_url="http://identity-test/api/identity/v1",
        session_orchestrator_url="http://orch-test:8006/api/session/v1",
        cors_origins="http://localhost",
        otel_enabled=False,
    )


@pytest_asyncio.fixture
async def db_engine_and_factory(
    settings: Settings,
) -> AsyncIterator[tuple[object, async_sessionmaker[AsyncSession]]]:
    """Fresh in-memory SQLite engine for each test."""
    engine = create_async_engine(settings.db_dsn, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    db_module._engine = engine  # noqa: SLF001
    db_module.set_session_factory(factory)
    try:
        yield engine, factory
    finally:
        db_module.set_session_factory(None)
        db_module._engine = None  # noqa: SLF001
        await engine.dispose()


@pytest.fixture
def client(
    settings: Settings,
    db_engine_and_factory: tuple,  # ensures DB is initialised before the app starts
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    from agent_bff import main as main_module

    async def _noop_init(*_a: object, **_kw: object) -> None:
        return None

    async def _noop_close() -> None:
        return None

    monkeypatch.setattr(main_module, "init_db", _noop_init)
    monkeypatch.setattr(main_module, "close_db", _noop_close)

    app = create_app(settings)
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def _make_cookie(settings: Settings, user_id: str, tenant_id: str = TENANT) -> dict[str, str]:
    from foundation_sdk import CookieSigner, CookieSignerSettings

    signer = CookieSigner(
        CookieSignerSettings(
            secret_key=settings.cookie_secret,
            salt="agent-bff-session",
            max_age_seconds=settings.cookie_max_age,
        )
    )
    token = signer.sign(
        {"user_id": user_id, "tenant_id": tenant_id, "access_token": "fake-jwt"}
    )
    return {settings.cookie_name: token}


# ── Tests ──────────────────────────────────────────────────────────────────────

def test_get_preferences_unauthenticated(client: TestClient) -> None:
    resp = client.get("/api/bff/v1/notifications/preferences")
    assert resp.status_code in (401, 403)


def test_put_preferences_unauthenticated(client: TestClient) -> None:
    resp = client.put(
        "/api/bff/v1/notifications/preferences",
        json={"items": []},
    )
    assert resp.status_code in (401, 403)


def test_get_returns_all_defaults_when_no_rows(
    client: TestClient, settings: Settings
) -> None:
    """GET with no DB rows → 6 events with their per-event defaults."""
    cookie = _make_cookie(settings, USER_A)
    resp = client.get("/api/bff/v1/notifications/preferences", cookies=cookie)
    assert resp.status_code == 200, resp.text

    body = resp.json()
    items = body["items"]
    assert len(items) == 6  # all canonical events

    keys = {i["event_key"] for i in items}
    assert "task_completed" in keys
    assert "budget_80pct" in keys
    assert "weekly_digest" in keys

    # task_completed defaults: email=True, push=True
    tc = next(i for i in items if i["event_key"] == "task_completed")
    assert tc["email_enabled"] is True
    assert tc["push_enabled"] is True

    # budget_80pct default: email=True, push=False
    b80 = next(i for i in items if i["event_key"] == "budget_80pct")
    assert b80["email_enabled"] is True
    assert b80["push_enabled"] is False


def test_put_and_get_roundtrip(
    client: TestClient, settings: Settings
) -> None:
    """PUT then GET: updated values are persisted."""
    cookie = _make_cookie(settings, USER_A)

    put_body = {
        "items": [
            {"event_key": "task_completed", "email_enabled": False, "push_enabled": False},
            {"event_key": "budget_80pct",   "email_enabled": False, "push_enabled": True},
        ]
    }
    put_resp = client.put(
        "/api/bff/v1/notifications/preferences",
        json=put_body,
        cookies=cookie,
    )
    assert put_resp.status_code == 200, put_resp.text

    get_resp = client.get("/api/bff/v1/notifications/preferences", cookies=cookie)
    assert get_resp.status_code == 200, get_resp.text

    items = {i["event_key"]: i for i in get_resp.json()["items"]}

    # Updated values
    assert items["task_completed"]["email_enabled"] is False
    assert items["task_completed"]["push_enabled"] is False
    assert items["budget_80pct"]["email_enabled"] is False
    assert items["budget_80pct"]["push_enabled"] is True

    # Untouched event keeps its default
    assert items["needs_review"]["email_enabled"] is True
    assert items["needs_review"]["push_enabled"] is True


def test_put_full_payload(client: TestClient, settings: Settings) -> None:
    """PUT with all 6 events → all stored, GET reflects each one."""
    cookie = _make_cookie(settings, USER_A)
    all_events = [
        "task_completed", "needs_review", "budget_80pct",
        "budget_hit", "worker_error", "weekly_digest",
    ]
    items_payload = [
        {"event_key": k, "email_enabled": False, "push_enabled": False}
        for k in all_events
    ]
    resp = client.put(
        "/api/bff/v1/notifications/preferences",
        json={"items": items_payload},
        cookies=cookie,
    )
    assert resp.status_code == 200, resp.text
    items = {i["event_key"]: i for i in resp.json()["items"]}
    for k in all_events:
        assert items[k]["email_enabled"] is False
        assert items[k]["push_enabled"] is False


def test_put_unknown_event_key_ignored(client: TestClient, settings: Settings) -> None:
    """Unknown event keys silently ignored — no 400, other events not affected."""
    cookie = _make_cookie(settings, USER_A)
    resp = client.put(
        "/api/bff/v1/notifications/preferences",
        json={
            "items": [
                {"event_key": "no_such_event_xyz", "email_enabled": False, "push_enabled": False},
                {"event_key": "task_completed", "email_enabled": True, "push_enabled": False},
            ]
        },
        cookies=cookie,
    )
    assert resp.status_code == 200, resp.text
    items = {i["event_key"]: i for i in resp.json()["items"]}
    # Unknown key not in response
    assert "no_such_event_xyz" not in items
    # Known key was stored
    assert items["task_completed"]["push_enabled"] is False


def test_two_users_are_isolated(client: TestClient, settings: Settings) -> None:
    """User A's update must not affect User B's preferences."""
    cookie_a = _make_cookie(settings, USER_A)
    cookie_b = _make_cookie(settings, USER_B)

    # User A turns off all notifications for task_completed
    client.put(
        "/api/bff/v1/notifications/preferences",
        json={"items": [
            {"event_key": "task_completed", "email_enabled": False, "push_enabled": False}
        ]},
        cookies=cookie_a,
    )

    # User B fetches — should still see defaults
    resp_b = client.get("/api/bff/v1/notifications/preferences", cookies=cookie_b)
    assert resp_b.status_code == 200, resp_b.text
    items_b = {i["event_key"]: i for i in resp_b.json()["items"]}
    assert items_b["task_completed"]["email_enabled"] is True
    assert items_b["task_completed"]["push_enabled"] is True


def test_put_idempotent(client: TestClient, settings: Settings) -> None:
    """Calling PUT twice with different values → latest value wins."""
    cookie = _make_cookie(settings, USER_A)
    for email_val in (True, False, True):
        resp = client.put(
            "/api/bff/v1/notifications/preferences",
            json={"items": [
                {"event_key": "task_completed", "email_enabled": email_val, "push_enabled": True}
            ]},
            cookies=cookie,
        )
        assert resp.status_code == 200, resp.text

    get_resp = client.get("/api/bff/v1/notifications/preferences", cookies=cookie)
    items = {i["event_key"]: i for i in get_resp.json()["items"]}
    assert items["task_completed"]["email_enabled"] is True  # last value was True
