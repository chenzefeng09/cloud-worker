"""Browser session lifecycle: create at login, resolve + refresh per request,
revoke at logout.

Keycloak access tokens live five minutes; a web session lives until foundation
identity's idle / absolute timeout ends it (or the user signs out).  The
session cookie only names the session (see
:mod:`agent_bff.domain.web_sessions`); :meth:`WebSessionManager.resolve` turns
it into an access token for the current request, refreshing it first when it
has less than ``session_refresh_skew_seconds`` left.  The cookie itself never
changes on refresh, so long-lived responses (SSE relays) are unaffected: a
stream keeps the connection it opened, and its reconnect picks up the current
token.

Concurrency
-----------
Identity rotates refresh tokens and treats a second use of a spent one as a
replay — it then revokes every session the user has.  So at most one refresh
may run per session, and every other request must use its result instead of
refreshing again:

* within a process, an ``asyncio.Lock`` per session serialises refreshes;
* across replicas, the refresh runs in a transaction holding
  ``SELECT … FOR UPDATE`` on the session row (bounded by ``lock_timeout``);
* once both are held the row is read again — if another request refreshed it
  meanwhile, that token is used as-is.

The refresh runs in its own task behind ``asyncio.shield``: a client that
disconnects mid-refresh must not abandon a rotation identity has already
performed, or the stored refresh token would be the spent one.

Failures
--------
* identity answers 200 → the rotated tokens are stored.
* the request never reached identity (connect failure), identity reports that
  one of *its* dependencies failed (``FOUNDATION.IDENTITY.DEPENDENCY.*`` with
  502/503 — the refresh token is left unspent), or 429 → transient: the session
  is kept, the current access token is used while it is still valid, and
  otherwise the request gets 503 and a later request retries.
* anything else — 401 (expired, revoked, idle / absolute timeout, reused), other
  4xx / 5xx, or a timeout after the request was sent — ends the session: the
  row is deleted and the request is unauthenticated (401).  A refresh token
  whose fate is unknown is never presented again.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import weakref
from collections.abc import Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Any

import httpx
from jose import jwt
from jose.exceptions import JWTError
from sqlalchemy import delete, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from agent_bff.api._http import foundation_headers
from agent_bff.api._jwt import clearance_from_token
from agent_bff.config import Settings
from agent_bff.domain import db as db_module
from agent_bff.domain.web_sessions import (
    ACCESS_TOKEN,
    REFRESH_TOKEN,
    SWEEP_ACTOR,
    TokenCipher,
    TokenDecryptError,
    WebSession,
    as_utc,
    hash_session_id,
    new_session_id,
    token_binding,
)
from agent_bff.errors import BFFError

__all__ = [
    "IdentitySessionClient",
    "RefreshKind",
    "RefreshResult",
    "Resolution",
    "SessionState",
    "SessionUnavailableError",
    "WebSessionManager",
]

logger = logging.getLogger(__name__)

# What a database round-trip can raise: ORM / driver errors, plus the raw
# socket errors and timeouts the async driver may surface while connecting.
_DB_ERRORS: tuple[type[BaseException], ...] = (SQLAlchemyError, OSError, TimeoutError)

# Identity errors raised before the refresh token was spent (see
# identity's refresh_session: cache / Keycloak failures release the reuse mark).
_IDENTITY_DEPENDENCY_CODE = "FOUNDATION.IDENTITY.DEPENDENCY."
# Used when neither the JWT nor identity says when the access token expires.
_DEFAULT_ACCESS_TTL = timedelta(seconds=300)
# The opportunistic expiry sweep runs at most this often per process.
_SWEEP_INTERVAL_SECONDS = 600.0


class SessionState(Enum):
    """How the current request's session cookie resolved."""

    NONE = "none"  # no usable session cookie (or the machine path)
    AUTHENTICATED = "authenticated"
    ENDED = "ended"  # validly signed cookie, but its session is over
    UNAVAILABLE = "unavailable"  # live session that could not be resolved right now


class SessionUnavailableError(BFFError):
    """The session exists but cannot be used right now (store / identity down)."""

    def __init__(self, detail: str = "Session temporarily unavailable, please retry") -> None:
        super().__init__(detail, code="AGENT.BFF.SESSION_UNAVAILABLE", status=503)


@dataclass(frozen=True, slots=True)
class Resolution:
    state: SessionState
    access_token: str = ""
    clearance: str = ""


_ENDED = Resolution(SessionState.ENDED)
_UNAVAILABLE = Resolution(SessionState.UNAVAILABLE)


class RefreshKind(Enum):
    ROTATED = "rotated"
    TRANSIENT = "transient"  # nothing was spent; retrying later is safe
    FAILED = "failed"  # the session is over (or its refresh token's fate is unknown)


@dataclass(frozen=True, slots=True)
class RefreshResult:
    kind: RefreshKind
    session: Mapping[str, Any] | None = None
    reason: str = ""


class IdentitySessionClient:
    """The identity calls a web session needs after login: refresh and revoke."""

    def __init__(self, identity_url: str, *, timeout_seconds: float) -> None:
        self._url = identity_url.rstrip("/")
        self._timeout = httpx.Timeout(timeout_seconds, connect=min(5.0, timeout_seconds))

    async def refresh(self, *, refresh_token: str, tenant_id: str, user_id: str) -> RefreshResult:
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(
                    f"{self._url}/sessions/current/refresh",
                    json={"refresh_token": refresh_token},
                    headers=foundation_headers(tenant_id, user_id, ""),
                )
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
            # Never reached identity: the refresh token is still unspent.
            return RefreshResult(
                RefreshKind.TRANSIENT, reason=f"identity unreachable ({type(exc).__name__})"
            )
        except httpx.HTTPError as exc:
            # Sent, but no answer: identity may already have rotated the token.
            return RefreshResult(
                RefreshKind.FAILED, reason=f"no answer from identity ({type(exc).__name__})"
            )

        if resp.status_code == 200:
            body = _json_object(resp)
            if (
                body is not None
                and isinstance(body.get("access_token"), str)
                and body["access_token"]
            ):
                return RefreshResult(RefreshKind.ROTATED, session=body)
            return RefreshResult(RefreshKind.FAILED, reason="malformed refresh response")

        code = _problem_code(resp)
        reason = f"identity {resp.status_code} {code}".strip()
        if resp.status_code == 429 or (
            resp.status_code in (502, 503) and code.startswith(_IDENTITY_DEPENDENCY_CODE)
        ):
            return RefreshResult(RefreshKind.TRANSIENT, reason=reason)
        return RefreshResult(RefreshKind.FAILED, reason=reason)

    async def revoke(self, *, refresh_token: str, tenant_id: str, user_id: str) -> None:
        """End the session at identity (Keycloak logout + token blacklist).

        Best-effort: the BFF-side session is already gone when this runs, so a
        failure only leaves the Keycloak session to its idle timeout.
        """
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.request(
                    "DELETE",
                    f"{self._url}/sessions/current",
                    json={"refresh_token": refresh_token},
                    headers=foundation_headers(tenant_id, user_id, ""),
                )
        except httpx.HTTPError as exc:
            logger.info("web session: identity revoke failed (%s)", type(exc).__name__)
            return
        if resp.status_code >= 400:
            logger.info("web session: identity revoke returned %s", resp.status_code)


@dataclass(frozen=True, slots=True)
class _RefreshStep:
    resolution: Resolution
    # True when the step rotated or deleted the row, i.e. the transaction's
    # commit carries an outcome that must not be lost.
    changed: bool = False


class WebSessionManager:
    """Creates, resolves (refreshing as needed) and revokes web sessions."""

    def __init__(
        self,
        *,
        cipher: TokenCipher,
        identity: IdentitySessionClient,
        session_max_age_seconds: int,
        refresh_skew_seconds: float,
        lock_timeout_seconds: float,
    ) -> None:
        self._cipher = cipher
        self._identity = identity
        self._max_age = timedelta(seconds=session_max_age_seconds)
        self._skew = timedelta(seconds=refresh_skew_seconds)
        self._lock_timeout_ms = max(1, int(lock_timeout_seconds * 1000))
        self._locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()
        self._next_sweep = 0.0

    @classmethod
    def from_settings(cls, settings: Settings) -> WebSessionManager:
        timeout = settings.session_refresh_timeout_seconds
        return cls(
            cipher=TokenCipher(
                _split_keys(settings.session_token_keys),
                fallback_secret=settings.cookie_secret,
            ),
            identity=IdentitySessionClient(
                settings.foundation_identity_url, timeout_seconds=timeout
            ),
            session_max_age_seconds=settings.cookie_max_age,
            refresh_skew_seconds=settings.session_refresh_skew_seconds,
            # Outlast one in-flight refresh on another replica (connect + read).
            lock_timeout_seconds=timeout + 10,
        )

    # ------------------------------------------------------------------ login
    async def create(self, session: Mapping[str, Any]) -> str:
        """Store a freshly issued identity session; return its cookie session id.

        ``session`` is identity's ``POST /sessions`` response.  Raises
        :class:`SessionUnavailableError` when it cannot be stored — the identity
        session just created is then revoked again (best-effort).
        """
        tenant_id = str(session["tenant_id"])
        user_id = str(session["user_id"])
        access_token = str(session["access_token"])
        refresh_token = str(session.get("refresh_token") or "")
        now = _now()
        session_id = new_session_id()
        row = WebSession(
            id=hash_session_id(session_id),
            tenant_id=tenant_id,
            user_id=user_id,
            access_expires_at=_access_expiry(access_token, session.get("expires_at"), now),
            clearance=clearance_from_token(access_token),
            created_at=now,
            expires_at=now + self._max_age,
        )
        row.access_token_enc = self._cipher.encrypt(
            access_token, binding=token_binding(row, ACCESS_TOKEN)
        )
        row.refresh_token_enc = (
            self._cipher.encrypt(refresh_token, binding=token_binding(row, REFRESH_TOKEN))
            if refresh_token
            else None
        )
        if not refresh_token:
            logger.warning(
                "web session: identity issued no refresh token; the session will end "
                "when its access token expires (is use.refresh.tokens enabled on the "
                "identity client?)"
            )
        try:
            async with _transaction(tenant_id, user_id) as db:
                db.add(row)
        except _DB_ERRORS as exc:
            logger.error("web session: cannot store a new session (%s)", type(exc).__name__)
            if refresh_token:
                await self._identity.revoke(
                    refresh_token=refresh_token, tenant_id=tenant_id, user_id=user_id
                )
            raise SessionUnavailableError(
                "Sign-in is temporarily unavailable, please retry"
            ) from exc
        await self._maybe_sweep(tenant_id, user_id)
        return session_id

    # ---------------------------------------------------------------- request
    async def resolve(self, *, session_id: str, tenant_id: str, user_id: str) -> Resolution:
        """Access token for this request, refreshed first if it is about to expire."""
        row_id = hash_session_id(session_id)
        try:
            async with _transaction(tenant_id, user_id) as db:
                row = await db.get(WebSession, row_id)
        except _DB_ERRORS as exc:
            logger.warning("web session: lookup failed (%s)", type(exc).__name__)
            return _UNAVAILABLE
        if row is None or row.tenant_id != tenant_id or row.user_id != user_id:
            return _ENDED

        now = _now()
        if as_utc(row.expires_at) <= now:
            await self._discard(row_id, tenant_id, user_id)
            return _ENDED
        try:
            access_token = self._cipher.decrypt(
                row.access_token_enc, binding=token_binding(row, ACCESS_TOKEN)
            )
        except TokenDecryptError:
            logger.warning("web session: stored token unreadable, ending the session")
            await self._discard(row_id, tenant_id, user_id)
            return _ENDED

        access_expires_at = as_utc(row.access_expires_at)
        current = Resolution(SessionState.AUTHENTICATED, access_token, row.clearance)
        if access_expires_at - now > self._skew:
            return current
        if row.refresh_token_enc is None:
            if access_expires_at > now:
                return current
            await self._discard(row_id, tenant_id, user_id)
            return _ENDED

        # What to use if the session row cannot even be locked right now.
        fallback = current if access_expires_at > now else _UNAVAILABLE
        task = asyncio.ensure_future(self._refresh(row_id, tenant_id, user_id, fallback))
        task.add_done_callback(_consume_exception)
        return await asyncio.shield(task)

    async def _refresh(
        self, row_id: str, tenant_id: str, user_id: str, fallback: Resolution
    ) -> Resolution:
        lock = self._locks.get(row_id)
        if lock is None:
            lock = self._locks[row_id] = asyncio.Lock()
        async with lock:
            step: _RefreshStep | None = None
            try:
                async with _transaction(tenant_id, user_id) as db:
                    step = await self._refresh_locked(db, row_id, tenant_id, user_id)
            except _DB_ERRORS as exc:
                if step is None:
                    # Could not read / lock the row: nothing was sent to identity.
                    logger.warning(
                        "web session: refresh skipped, session row unavailable (%s)",
                        type(exc).__name__,
                    )
                    return fallback
                if step.changed:
                    # Identity's outcome (a rotation, or the end of the session)
                    # was not stored.  The stored refresh token may be spent, so
                    # it must never be presented again.
                    logger.error(
                        "web session: refresh outcome not persisted (%s), ending the session",
                        type(exc).__name__,
                    )
                    await self._discard(row_id, tenant_id, user_id)
            assert step is not None
            return step.resolution

    async def _refresh_locked(
        self, db: AsyncSession, row_id: str, tenant_id: str, user_id: str
    ) -> _RefreshStep:
        await self._set_lock_timeout(db)
        row = (
            await db.execute(
                select(WebSession)
                .where(WebSession.id == row_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if row is None:  # signed out or ended by a concurrent request
            return _RefreshStep(_ENDED)

        now = _now()
        access_expires_at = as_utc(row.access_expires_at)
        try:
            access_token = self._cipher.decrypt(
                row.access_token_enc, binding=token_binding(row, ACCESS_TOKEN)
            )
            refresh_token = (
                self._cipher.decrypt(
                    row.refresh_token_enc, binding=token_binding(row, REFRESH_TOKEN)
                )
                if row.refresh_token_enc is not None
                else ""
            )
        except TokenDecryptError:
            logger.warning("web session: stored token unreadable, ending the session")
            await db.delete(row)
            return _RefreshStep(_ENDED, changed=True)

        current = Resolution(SessionState.AUTHENTICATED, access_token, row.clearance)
        if access_expires_at - now > self._skew:
            # Another request refreshed it while this one waited.
            return _RefreshStep(current)
        if not refresh_token:
            if access_expires_at > now:
                return _RefreshStep(current)
            await db.delete(row)
            return _RefreshStep(_ENDED, changed=True)

        result = await self._identity.refresh(
            refresh_token=refresh_token, tenant_id=tenant_id, user_id=user_id
        )
        if result.kind is RefreshKind.ROTATED and result.session is not None:
            body = result.session
            if (
                str(body.get("user_id") or user_id) != user_id
                or str(body.get("tenant_id") or tenant_id) != tenant_id
            ):
                logger.error("web session: refresh returned another identity, ending the session")
                await db.delete(row)
                return _RefreshStep(_ENDED, changed=True)
            new_access = str(body["access_token"])
            new_refresh = str(body.get("refresh_token") or "")
            row.access_token_enc = self._cipher.encrypt(
                new_access, binding=token_binding(row, ACCESS_TOKEN)
            )
            # Identity marks every presented token as used, so without a new
            # one the session simply ends with this access token.
            row.refresh_token_enc = (
                self._cipher.encrypt(new_refresh, binding=token_binding(row, REFRESH_TOKEN))
                if new_refresh
                else None
            )
            row.access_expires_at = _access_expiry(new_access, body.get("expires_at"), now)
            row.clearance = clearance_from_token(new_access)
            row.refreshed_at = now
            return _RefreshStep(
                Resolution(SessionState.AUTHENTICATED, new_access, row.clearance),
                changed=True,
            )
        if result.kind is RefreshKind.TRANSIENT:
            logger.warning("web session: refresh deferred: %s", result.reason)
            return _RefreshStep(current if access_expires_at > now else _UNAVAILABLE)
        logger.info("web session ended: refresh refused: %s", result.reason)
        await db.delete(row)
        return _RefreshStep(_ENDED, changed=True)

    # ----------------------------------------------------------------- logout
    async def revoke(self, *, session_id: str, tenant_id: str, user_id: str) -> None:
        """Sign out: delete the session, then end it at identity (best-effort)."""
        row_id = hash_session_id(session_id)
        refresh_token = ""
        try:
            async with _transaction(tenant_id, user_id) as db:
                await self._set_lock_timeout(db)
                row = (
                    await db.execute(
                        select(WebSession)
                        .where(WebSession.id == row_id)
                        .with_for_update()
                        .execution_options(populate_existing=True)
                    )
                ).scalar_one_or_none()
                if row is None or row.tenant_id != tenant_id or row.user_id != user_id:
                    return
                if row.refresh_token_enc is not None:
                    with contextlib.suppress(TokenDecryptError):
                        refresh_token = self._cipher.decrypt(
                            row.refresh_token_enc, binding=token_binding(row, REFRESH_TOKEN)
                        )
                await db.delete(row)
        except _DB_ERRORS as exc:
            logger.warning(
                "web session: sign-out could not delete the session (%s)", type(exc).__name__
            )
            return
        if refresh_token:
            await self._identity.revoke(
                refresh_token=refresh_token, tenant_id=tenant_id, user_id=user_id
            )

    # ---------------------------------------------------------------- helpers
    async def _set_lock_timeout(self, db: AsyncSession) -> None:
        bind = db.get_bind()
        if bind.dialect.name == "postgresql":
            await db.execute(text(f"SET LOCAL lock_timeout = '{self._lock_timeout_ms}ms'"))

    async def _discard(self, row_id: str, tenant_id: str, user_id: str) -> None:
        try:
            async with _transaction(tenant_id, user_id) as db:
                await db.execute(
                    delete(WebSession)
                    .where(WebSession.id == row_id)
                    .execution_options(synchronize_session=False)
                )
        except _DB_ERRORS as exc:
            logger.warning(
                "web session: could not delete an ended session (%s)", type(exc).__name__
            )

    async def _maybe_sweep(self, tenant_id: str, user_id: str) -> None:
        """Delete sessions past their hard expiry (throttled, best-effort)."""
        if time.monotonic() < self._next_sweep:
            return
        self._next_sweep = time.monotonic() + _SWEEP_INTERVAL_SECONDS
        try:
            async with _transaction(tenant_id, user_id, actor=SWEEP_ACTOR) as db:
                await db.execute(
                    delete(WebSession)
                    .where(WebSession.expires_at < _now())
                    .execution_options(synchronize_session=False)
                )
        except _DB_ERRORS as exc:
            logger.info("web session: expiry sweep skipped (%s)", type(exc).__name__)


def _transaction(
    tenant_id: str, user_id: str, actor: str = "USER"
) -> AbstractAsyncContextManager[AsyncSession]:
    """One RLS-scoped transaction (commit on success, rollback on error)."""
    return contextlib.asynccontextmanager(db_module.get_session)(
        tenant_id=tenant_id, user_id=user_id, actor=actor
    )


def _now() -> datetime:
    return datetime.now(UTC)


def _split_keys(raw: str) -> list[str]:
    return [key.strip() for key in raw.split(",") if key.strip()]


def _access_expiry(access_token: str, reported: object, now: datetime) -> datetime:
    """When downstream services stop accepting ``access_token``.

    The earlier of the JWT ``exp`` claim and identity's reported
    ``expires_at``; five minutes from now when neither is available.
    """
    candidates: list[datetime] = []
    try:
        exp = jwt.get_unverified_claims(access_token).get("exp")
        if isinstance(exp, int | float):
            candidates.append(datetime.fromtimestamp(exp, UTC))
    except (JWTError, ValueError, TypeError, OverflowError, OSError):
        pass
    if isinstance(reported, str) and reported:
        with contextlib.suppress(ValueError):
            candidates.append(as_utc(datetime.fromisoformat(reported.replace("Z", "+00:00"))))
    return min(candidates) if candidates else now + _DEFAULT_ACCESS_TTL


def _json_object(resp: httpx.Response) -> dict[str, Any] | None:
    try:
        body = resp.json()
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


def _problem_code(resp: httpx.Response) -> str:
    body = _json_object(resp)
    code = body.get("code") if body is not None else None
    return code if isinstance(code, str) else ""


def _consume_exception(task: asyncio.Future[Any]) -> None:
    # The refresh task is shielded; if its awaiting request was cancelled,
    # nobody else retrieves its exception.
    if not task.cancelled():
        task.exception()
