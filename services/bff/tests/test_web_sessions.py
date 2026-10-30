"""Server-side web sessions: login stores the tokens, requests refresh them
before they expire, logout revokes them.

The access token Keycloak issues lives five minutes; these tests pin that a
web session outlives it (refresh), that concurrent requests trigger exactly
one refresh (identity treats a reused refresh token as a replay), that refusals
end the session with a 401 while outages answer 503 without spending the
refresh token, and that every login flow — password, SSO token, SSO redirect —
creates the same kind of session.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from sqlalchemy import create_engine, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_bff.api.middleware import get_web_session_id
from agent_bff.api.web_session import (
    IdentitySessionClient,
    RefreshKind,
    SessionState,
    WebSessionManager,
)
from agent_bff.config import Settings
from agent_bff.domain.db import set_session_factory
from agent_bff.domain.web_sessions import (
    ACCESS_TOKEN,
    REFRESH_TOKEN,
    TokenCipher,
    TokenDecryptError,
    WebSession,
    hash_session_id,
    token_binding,
)
from agent_bff.main import create_app
from tests.web_session_support import (
    REAL_ASYNC_CLIENT,
    TENANT,
    USER,
    FakeIdentity,
    all_rows,
    make_token,
    sqlite_session_factory,
)

PREFIX = "/api/bff/v1"
IDENTITY_URL = "http://identity-test/api/identity/v1"
ACCOUNT_SETUP_REQUIRED = "FOUNDATION.IDENTITY.AUTH.ACCOUNT_SETUP_REQUIRED"


@dataclass
class Env:
    app: FastAPI
    client: httpx.AsyncClient
    identity: FakeIdentity
    db: async_sessionmaker[AsyncSession]

    def cookie_payload(self) -> dict:
        return self.app.state.cookie_signer.unsign(self.client.cookies["ap_session"])

    async def login(self) -> httpx.Response:
        resp = await self.client.post(
            f"{PREFIX}/auth/login", json={"email": "u@example.com", "password": "pw"}
        )
        assert resp.status_code == 200, resp.text
        return resp

    async def list_sessions(self, **kwargs) -> httpx.Response:
        # Any require_auth endpoint will do; this one forwards the bearer token.
        return await self.client.get(f"{PREFIX}/auth/sessions", **kwargs)


@pytest.fixture
async def env(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[Env]:
    db = sqlite_session_factory(tmp_path / "bff.db")
    identity = FakeIdentity()
    identity.install(monkeypatch)
    app = create_app(settings)
    async with REAL_ASYNC_CLIENT(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        yield Env(app=app, client=client, identity=identity, db=db)
    set_session_factory(None)


async def _set_row(env: Env, **values) -> None:
    async with env.db() as session, session.begin():
        await session.execute(update(WebSession).values(**values))


def _drop_store(path: Path) -> None:
    """Make the session store fail: its table disappears."""
    engine = create_engine(f"sqlite:///{path}")
    WebSession.__table__.drop(engine)  # type: ignore[attr-defined]
    engine.dispose()


# --------------------------------------------------------------------------- #
# Login: tokens stay server-side, the cookie only names the session
# --------------------------------------------------------------------------- #
async def test_password_login_stores_tokens_server_side(env: Env) -> None:
    resp = await env.login()

    payload = env.cookie_payload()
    assert set(payload) == {"sid", "tenant_id", "user_id"}
    assert (payload["tenant_id"], payload["user_id"]) == (TENANT, USER)
    assert resp.json()["user_id"] == USER

    [row] = await all_rows(env.db)
    issued = env.identity.issued[0]
    assert row.id == hash_session_id(payload["sid"])  # raw id is not stored
    assert issued["access_token"] not in row.access_token_enc
    assert row.refresh_token_enc is not None
    assert issued["refresh_token"] not in row.refresh_token_enc
    assert row.clearance == "L3"

    # Requests authenticate with the stored token.
    assert (await env.list_sessions()).status_code == 200
    assert env.identity.bearers("GET", "/sessions") == [f"Bearer {issued['access_token']}"]


async def test_sso_token_login_starts_the_same_session(env: Env) -> None:
    resp = await env.client.post(
        f"{PREFIX}/auth/login/sso", json={"access_token": "idp-token", "provider_id": "corp"}
    )
    assert resp.status_code == 200, resp.text
    assert "sid" in env.cookie_payload()
    assert len(await all_rows(env.db)) == 1


async def test_sso_callback_starts_the_same_session(env: Env) -> None:
    state = env.app.state.cookie_signer.sign(
        {"p": "sso_corp", "o": "http://localhost", "n": "0123456789abcdef"}
    )
    resp = await env.client.get(
        f"{PREFIX}/auth/sso/callback", params={"code": "auth-code", "state": state}
    )

    assert resp.status_code == 302
    assert resp.headers["location"] == "http://localhost/home"
    assert "sid" in env.cookie_payload()
    assert len(await all_rows(env.db)) == 1
    login = next(r for r in env.identity.requests if r.url.path.endswith("/sessions"))
    assert b'"method":"SSO_CODE"' in login.content.replace(b" ", b"")


async def test_relogin_signs_out_the_previous_session(env: Env) -> None:
    await env.login()
    await env.login()

    [row] = await all_rows(env.db)
    assert row.id == hash_session_id(env.cookie_payload()["sid"])
    [revoke] = env.identity.revoked
    assert revoke.content and env.identity.issued[0]["refresh_token"].encode() in revoke.content


async def test_login_fails_closed_when_the_session_store_is_down(env: Env, tmp_path: Path) -> None:
    _drop_store(tmp_path / "bff.db")

    resp = await env.client.post(
        f"{PREFIX}/auth/login", json={"email": "u@example.com", "password": "pw"}
    )

    assert resp.status_code == 503
    assert resp.json()["code"] == "AGENT.BFF.SESSION_UNAVAILABLE"
    assert "ap_session" not in env.client.cookies
    # The identity session that could not be stored is ended again.
    [revoke] = env.identity.revoked
    assert env.identity.issued[0]["refresh_token"].encode() in revoke.content


# --------------------------------------------------------------------------- #
# Login errors
# --------------------------------------------------------------------------- #
async def test_login_passes_through_account_setup_required(env: Env) -> None:
    env.identity.login_status = 401
    env.identity.login_body = {
        "code": ACCOUNT_SETUP_REQUIRED,
        "title": "Account setup required",
        "status": 401,
        "detail": "Account is not fully set up (required actions pending)",
        "user_message": "账号需要先完成更新才能登录。",
    }

    resp = await env.client.post(
        f"{PREFIX}/auth/login", json={"email": "u@example.com", "password": "pw"}
    )

    assert resp.status_code == 401
    body = resp.json()["detail"]
    assert body["detail"]["code"] == ACCOUNT_SETUP_REQUIRED
    assert body["user_message"] == "账号需要先完成更新才能登录。"
    assert await all_rows(env.db) == []
    assert "ap_session" not in env.client.cookies


async def test_login_wrong_password_stays_generic(env: Env) -> None:
    env.identity.login_status = 401
    env.identity.login_body = {"code": "FOUNDATION.IDENTITY.AUTH.INVALID_CREDENTIALS"}

    resp = await env.client.post(
        f"{PREFIX}/auth/login", json={"email": "u@example.com", "password": "bad"}
    )

    assert resp.status_code == 401
    assert resp.json()["code"] == "AGENT.BFF.AUTH_FAILED"
    assert resp.json()["detail"] == "Invalid credentials"


async def test_login_identity_outage_is_not_reported_as_bad_credentials(env: Env) -> None:
    env.identity.login_status = 503
    env.identity.login_body = {"code": "FOUNDATION.IDENTITY.DEPENDENCY.KEYCLOAK_UNAVAILABLE"}

    resp = await env.client.post(
        f"{PREFIX}/auth/login", json={"email": "u@example.com", "password": "pw"}
    )

    assert resp.status_code == 502


# --------------------------------------------------------------------------- #
# Refresh
# --------------------------------------------------------------------------- #
async def test_access_token_is_refreshed_shortly_before_it_expires(env: Env) -> None:
    env.identity.access_ttl = 30  # inside the 60 s refresh window
    await env.login()
    cookie = env.client.cookies["ap_session"]
    env.identity.access_ttl = 300

    resp = await env.list_sessions()

    assert resp.status_code == 200
    assert "set-cookie" not in resp.headers  # the cookie never changes on refresh
    assert env.identity.refresh_calls == 1
    login, rotated = env.identity.issued
    assert env.identity.refresh_bodies == [{"refresh_token": login["refresh_token"]}]
    assert env.identity.bearers("GET", "/sessions") == [f"Bearer {rotated['access_token']}"]

    [row] = await all_rows(env.db)
    assert row.refreshed_at is not None
    cipher = env.app.state.web_sessions._cipher
    assert (
        cipher.decrypt(row.refresh_token_enc, binding=token_binding(row, REFRESH_TOKEN))
        == rotated["refresh_token"]
    )

    # Fresh again: the next request does not refresh.
    assert (await env.list_sessions()).status_code == 200
    assert env.identity.refresh_calls == 1
    assert env.client.cookies["ap_session"] == cookie


async def test_concurrent_requests_share_a_single_refresh(env: Env) -> None:
    env.identity.access_ttl = 30
    await env.login()
    env.identity.access_ttl = 300
    gate = env.identity.refresh_gate = asyncio.Event()

    async def _release() -> None:
        while env.identity.refresh_calls == 0:
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)  # let the other requests pile up behind it
        gate.set()

    *responses, _ = await asyncio.gather(*(env.list_sessions() for _ in range(8)), _release())

    assert [r.status_code for r in responses] == [200] * 8
    assert env.identity.refresh_calls == 1
    rotated = env.identity.issued[1]["access_token"]
    assert set(env.identity.bearers("GET", "/sessions")) == {f"Bearer {rotated}"}


async def test_stream_relay_reconnects_with_the_refreshed_token(env: Env) -> None:
    """The SSE relay authenticates its upstream connection once, when it opens;
    the browser's reconnect carries the same cookie, so it is relayed with the
    token the session holds by then — refreshed if it was about to expire."""
    await env.login()
    cookie = env.client.cookies["ap_session"]
    stream_url = f"{PREFIX}/sessions/ses_1/stream"

    first = await env.client.get(stream_url)
    assert first.status_code == 200
    assert "event: ping" in first.text

    # The stream outlives its access token, then the browser reconnects.
    await _set_row(env, access_expires_at=datetime.now(UTC) + timedelta(seconds=10))
    second = await env.client.get(stream_url)

    assert second.status_code == 200
    assert "event: ping" in second.text
    assert env.identity.refresh_calls == 1
    login, rotated = env.identity.issued
    assert env.identity.bearers("GET", "/stream") == [
        f"Bearer {login['access_token']}",
        f"Bearer {rotated['access_token']}",
    ]
    assert env.client.cookies["ap_session"] == cookie


async def test_refused_refresh_ends_the_session(env: Env) -> None:
    env.identity.access_ttl = 30
    await env.login()
    env.identity.refresh_response = lambda _n: httpx.Response(
        401, json={"code": "FOUNDATION.IDENTITY.AUTH.TOKEN_EXPIRED"}
    )

    resp = await env.list_sessions()

    assert resp.status_code == 401
    assert resp.json()["detail"] == "Session expired"
    assert await all_rows(env.db) == []
    # Ended for good: no second attempt with that refresh token.
    assert (await env.list_sessions()).status_code == 401
    assert env.identity.refresh_calls == 1


async def test_identity_outage_keeps_the_session_and_its_refresh_token(env: Env) -> None:
    env.identity.access_ttl = 30
    await env.login()
    login = env.identity.issued[0]
    env.identity.refresh_response = lambda _n: httpx.Response(
        503, json={"code": "FOUNDATION.IDENTITY.DEPENDENCY.KEYCLOAK_UNAVAILABLE"}
    )

    # The access token still has ~30 s: keep serving with it.
    resp = await env.list_sessions()
    assert resp.status_code == 200
    assert env.identity.bearers("GET", "/sessions") == [f"Bearer {login['access_token']}"]

    # Once it has expired: 503 (retry), not 401 (sign in again).
    await _set_row(env, access_expires_at=datetime.now(UTC) - timedelta(seconds=1))
    resp = await env.list_sessions()
    assert resp.status_code == 503
    assert resp.json()["code"] == "AGENT.BFF.SESSION_UNAVAILABLE"
    assert len(await all_rows(env.db)) == 1

    # Identity recovers: the same, unspent refresh token is redeemed.
    env.identity.refresh_response = None
    assert (await env.list_sessions()).status_code == 200
    assert {b["refresh_token"] for b in env.identity.refresh_bodies} == {login["refresh_token"]}


async def test_session_without_refresh_token_ends_with_its_access_token(env: Env) -> None:
    env.identity.access_ttl = 300
    await env.login()
    await _set_row(
        env,
        refresh_token_enc=None,
        access_expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )

    assert (await env.list_sessions()).status_code == 401
    assert env.identity.refresh_calls == 0
    assert await all_rows(env.db) == []


# --------------------------------------------------------------------------- #
# Logout and cookie handling
# --------------------------------------------------------------------------- #
async def test_logout_revokes_the_session(env: Env) -> None:
    await env.login()
    cookie = env.client.cookies["ap_session"]

    resp = await env.client.post(f"{PREFIX}/auth/logout")

    assert resp.status_code == 204
    assert "ap_session=" in resp.headers["set-cookie"]
    assert await all_rows(env.db) == []
    [revoke] = env.identity.revoked
    assert env.identity.issued[0]["refresh_token"].encode() in revoke.content
    assert revoke.headers["x-tenant-id"] == TENANT
    assert revoke.headers["x-user-id"] == USER

    # A copy of the old cookie is worthless now.
    env.client.cookies.clear()
    env.client.cookies.set("ap_session", cookie)
    assert (await env.list_sessions()).status_code == 401


async def test_dead_session_cookie_does_not_fall_back_to_identity_headers(env: Env) -> None:
    await env.login()
    await _set_row(env, expires_at=datetime.now(UTC) - timedelta(seconds=1))
    spoofed = {"X-Tenant-Id": TENANT, "X-User-Id": USER}

    assert (await env.list_sessions(headers=spoofed)).status_code == 401
    env.client.cookies.clear()
    env.client.cookies.set("ap_session", "not-a-signed-cookie")
    assert (await env.list_sessions(headers=spoofed)).status_code == 401


async def test_a_request_knows_which_sign_in_it_came_from(env: Env) -> None:
    """``get_web_session_id`` names the session row (privacy erasure keeps it)."""
    seen: list[str] = []

    @env.app.get("/probe-web-session")
    async def _probe() -> dict:
        seen.append(get_web_session_id())
        return {}

    await env.login()
    await env.client.get("/probe-web-session")
    env.client.cookies.clear()
    await env.client.get("/probe-web-session")

    [row] = await all_rows(env.db)
    assert seen == [row.id, ""]


async def test_legacy_cookie_is_still_honoured(env: Env) -> None:
    legacy = env.app.state.cookie_signer.sign(
        {"user_id": USER, "tenant_id": TENANT, "access_token": "legacy-jwt"}
    )
    env.client.cookies.set("ap_session", legacy)

    assert (await env.list_sessions()).status_code == 200
    assert env.identity.bearers("GET", "/sessions") == ["Bearer legacy-jwt"]


# --------------------------------------------------------------------------- #
# Manager-level behaviour
# --------------------------------------------------------------------------- #
@pytest.fixture
async def manager(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[tuple[WebSessionManager, FakeIdentity, async_sessionmaker[AsyncSession]]]:
    db = sqlite_session_factory(tmp_path / "bff.db")
    identity = FakeIdentity()
    identity.install(monkeypatch)
    mgr = WebSessionManager(
        cipher=TokenCipher([Fernet.generate_key().decode()], fallback_secret=""),
        identity=IdentitySessionClient(IDENTITY_URL, timeout_seconds=5),
        session_max_age_seconds=86_400,
        refresh_skew_seconds=60,
        lock_timeout_seconds=5,
    )
    yield mgr, identity, db
    set_session_factory(None)


async def test_refresh_survives_the_request_being_cancelled(manager) -> None:
    mgr, identity, db = manager
    identity.access_ttl = 30
    sid = await mgr.create(identity.next_bundle())
    identity.access_ttl = 300
    gate = identity.refresh_gate = asyncio.Event()

    request = asyncio.ensure_future(mgr.resolve(session_id=sid, tenant_id=TENANT, user_id=USER))
    while identity.refresh_calls == 0:
        await asyncio.sleep(0.01)
    request.cancel()  # e.g. the browser went away mid-refresh
    gate.set()
    with pytest.raises(asyncio.CancelledError):
        await request

    # Identity rotated the token; the rotation must still be persisted.
    rotated = identity.issued[1]["refresh_token"]
    for _ in range(100):
        [row] = await all_rows(db)
        if row.refreshed_at is not None:
            break
        await asyncio.sleep(0.01)
    assert (
        mgr._cipher.decrypt(row.refresh_token_enc, binding=token_binding(row, REFRESH_TOKEN))
        == rotated
    )


async def test_stored_tokens_cannot_be_moved_between_sessions(manager) -> None:
    mgr, identity, db = manager
    sid_a = await mgr.create(identity.next_bundle())
    sid_b = await mgr.create(identity.next_bundle())
    rows = {row.id: row for row in await all_rows(db)}
    row_a, row_b = rows[hash_session_id(sid_a)], rows[hash_session_id(sid_b)]
    async with db() as session, session.begin():
        await session.execute(
            update(WebSession)
            .where(WebSession.id == row_a.id)
            .values(access_token_enc=row_b.access_token_enc)
        )

    tampered = await mgr.resolve(session_id=sid_a, tenant_id=TENANT, user_id=USER)
    intact = await mgr.resolve(session_id=sid_b, tenant_id=TENANT, user_id=USER)

    assert tampered.state is SessionState.ENDED
    assert intact.state is SessionState.AUTHENTICATED
    assert [r.id for r in await all_rows(db)] == [hash_session_id(sid_b)]


async def test_expired_sessions_end_and_are_swept(manager) -> None:
    mgr, identity, db = manager
    stale = await mgr.create(identity.next_bundle())
    async with db() as session, session.begin():
        await session.execute(
            update(WebSession).values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )

    assert (
        await mgr.resolve(session_id=stale, tenant_id=TENANT, user_id=USER)
    ).state is SessionState.ENDED
    assert await all_rows(db) == []

    # The opportunistic sweep at login removes other users' dead rows too.
    other = await mgr.create({**identity.next_bundle(), "user_id": "usr_OTHER"})
    async with db() as session, session.begin():
        await session.execute(
            update(WebSession).values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    mgr._next_sweep = 0.0
    await mgr.create(identity.next_bundle())
    rows = await all_rows(db)
    assert len(rows) == 1 and rows[0].id != hash_session_id(other)


async def test_lookup_failure_is_unavailable_not_signed_out(manager, tmp_path: Path) -> None:
    mgr, identity, _db = manager
    sid = await mgr.create(identity.next_bundle())
    _drop_store(tmp_path / "bff.db")

    result = await mgr.resolve(session_id=sid, tenant_id=TENANT, user_id=USER)
    assert result.state is SessionState.UNAVAILABLE


# --------------------------------------------------------------------------- #
# Identity refresh outcome classification
# --------------------------------------------------------------------------- #
def _respond(status: int, code: str = ""):
    return lambda _request: httpx.Response(status, json={"code": code} if code else {})


def _raise(exc: Exception):
    def _handler(_request: httpx.Request) -> httpx.Response:
        raise exc

    return _handler


@pytest.mark.parametrize(
    ("handler", "expected"),
    [
        (
            lambda _r: httpx.Response(200, json={"access_token": "a", "refresh_token": "r"}),
            RefreshKind.ROTATED,
        ),
        (_respond(200), RefreshKind.FAILED),
        (_respond(401, "FOUNDATION.IDENTITY.AUTH.REFRESH_TOKEN_REUSED"), RefreshKind.FAILED),
        (_respond(401, "FOUNDATION.IDENTITY.AUTH.TOKEN_EXPIRED"), RefreshKind.FAILED),
        (
            _respond(503, "FOUNDATION.IDENTITY.DEPENDENCY.KEYCLOAK_UNAVAILABLE"),
            RefreshKind.TRANSIENT,
        ),
        (_respond(502, "FOUNDATION.IDENTITY.DEPENDENCY.KEYCLOAK_ERROR"), RefreshKind.TRANSIENT),
        (_respond(503, "FOUNDATION.IDENTITY.DEPENDENCY.REDIS_UNAVAILABLE"), RefreshKind.TRANSIENT),
        (_respond(429), RefreshKind.TRANSIENT),
        (_respond(503), RefreshKind.FAILED),  # not identity's own word that nothing was spent
        (_respond(500, "FOUNDATION.IDENTITY.INTERNAL.UNEXPECTED"), RefreshKind.FAILED),
        (_raise(httpx.ConnectError("refused")), RefreshKind.TRANSIENT),
        (_raise(httpx.ReadTimeout("slow")), RefreshKind.FAILED),
    ],
)
async def test_refresh_outcome_classification(
    handler, expected: RefreshKind, monkeypatch: pytest.MonkeyPatch
) -> None:
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda *a, **kw: REAL_ASYNC_CLIENT(*a, transport=transport, **kw)
    )
    client = IdentitySessionClient(IDENTITY_URL, timeout_seconds=5)

    result = await client.refresh(refresh_token="rt", tenant_id=TENANT, user_id=USER)

    assert result.kind is expected


# --------------------------------------------------------------------------- #
# Token encryption
# --------------------------------------------------------------------------- #
def _row(row_id: str = "r1", user_id: str = USER) -> WebSession:
    return WebSession(id=row_id, tenant_id=TENANT, user_id=user_id)


def test_token_cipher_binds_ciphertext_to_its_row_and_kind() -> None:
    cipher = TokenCipher([Fernet.generate_key().decode()], fallback_secret="")
    blob = cipher.encrypt("secret-token", binding=token_binding(_row(), ACCESS_TOKEN))

    assert cipher.decrypt(blob, binding=token_binding(_row(), ACCESS_TOKEN)) == "secret-token"
    for other in (
        token_binding(_row(), REFRESH_TOKEN),
        token_binding(_row("r2"), ACCESS_TOKEN),
        token_binding(_row(user_id="usr_OTHER"), ACCESS_TOKEN),
    ):
        with pytest.raises(TokenDecryptError):
            cipher.decrypt(blob, binding=other)


def test_token_cipher_key_rotation() -> None:
    old, new = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    binding = token_binding(_row(), ACCESS_TOKEN)
    blob = TokenCipher([old], fallback_secret="").encrypt("t", binding=binding)

    rotated = TokenCipher([new, old], fallback_secret="")
    assert rotated.decrypt(blob, binding=binding) == "t"
    with pytest.raises(TokenDecryptError):
        TokenCipher([new], fallback_secret="").decrypt(blob, binding=binding)


def test_token_cipher_falls_back_to_a_key_derived_from_the_cookie_secret() -> None:
    binding = token_binding(_row(), ACCESS_TOKEN)
    blob = TokenCipher([], fallback_secret="cookie-secret-a").encrypt("t", binding=binding)

    assert TokenCipher([" "], fallback_secret="cookie-secret-a").decrypt(blob, binding=binding)
    with pytest.raises(TokenDecryptError):
        TokenCipher([], fallback_secret="cookie-secret-b").decrypt(blob, binding=binding)


def test_malformed_token_keys_fail_startup(settings: Settings) -> None:
    with pytest.raises(ValueError, match="BFF_SESSION_TOKEN_KEYS"):
        create_app(settings.model_copy(update={"session_token_keys": "not-a-fernet-key"}))


def test_access_expiry_prefers_the_earliest_known_deadline() -> None:
    from agent_bff.api.web_session import _access_expiry

    now = datetime.now(UTC)
    assert _access_expiry("opaque", None, now) == now + timedelta(seconds=300)
    soon = make_token("access", ttl=42)
    assert abs((_access_expiry(soon, None, now) - now).total_seconds() - 42) < 3
    reported = (now + timedelta(seconds=10)).isoformat()
    assert _access_expiry(soon, reported, now) == datetime.fromisoformat(reported)
