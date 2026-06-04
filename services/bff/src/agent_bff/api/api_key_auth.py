"""API key authentication dependency for BFF.

Usage (attach to any endpoint):

    from agent_bff.api.api_key_auth import require_api_key

    @router.get("/some-endpoint")
    async def my_endpoint(
        identity: ApiKeyIdentity = Depends(require_api_key()),
    ) -> ...:
        ...  # identity.user_id / identity.tenant_id are set

The dependency reads the key from:
  1. ``Authorization: Bearer <key>`` header
  2. ``X-API-Key: <key>`` header

If neither header is present → 401.
If the key is not found / revoked → 401.

Security
--------
- We hash the presented key with SHA-256 and look it up in ``api_keys.key_hash``.
- The lookup uses a raw DB session (no RLS context vars) combined with the
  ``api_keys_hash_lookup`` PERMISSIVE Postgres policy (migration 0002) which
  allows SELECT when no per-user context is set via the bypass policy.
  SQLite test sessions don't have RLS so the raw session works there too.
- On success, ``last_used_at`` is updated (best-effort; failures are swallowed
  so auth doesn't fail just because the update failed).
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select

from agent_bff.domain.db import get_raw_session
from agent_bff.domain.models import ApiKey

logger = logging.getLogger(__name__)

# HTTPBearer is optional here — we also support X-API-Key so we use the raw
# request.  auto_error=False so we can fall back to X-API-Key.
_bearer = HTTPBearer(auto_error=False)


def _extract_key(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None,
) -> str | None:
    """Return the raw API key from the request, or None if not present."""
    if credentials is not None:
        return credentials.credentials
    x_api_key = request.headers.get("X-API-Key")
    if x_api_key:
        return x_api_key
    return None


@dataclass
class ApiKeyIdentity:
    """Resolved identity from a valid API key."""

    key_id: str
    user_id: str
    tenant_id: str


def require_api_key() -> Callable[..., Coroutine[Any, Any, ApiKeyIdentity]]:
    """FastAPI dependency: validate an API key and return its identity.

    Returns an ``ApiKeyIdentity`` on success.  Raises 401 on any failure.
    """

    async def _require_api_key(
        request: Request,
        credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    ) -> ApiKeyIdentity:
        raw_key = _extract_key(request, credentials)
        if not raw_key:
            raise HTTPException(
                status_code=401,
                detail="API key required (Authorization: Bearer <key> or X-API-Key header)",
            )

        key_hash = hashlib.sha256(raw_key.encode()).hexdigest()

        try:
            identity = await _lookup_key(key_hash)
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("api_key_auth: DB lookup failed: %s", exc)
            raise HTTPException(status_code=401, detail="Invalid API key") from exc

        return identity

    return _require_api_key


async def _lookup_key(key_hash: str) -> ApiKeyIdentity:
    """Look up a key hash in the DB.  Returns identity or raises 401.

    Uses ``get_raw_session`` (no RLS context vars set) because we don't know
    the tenant/user before the lookup.  On SQLite (unit tests) there is no RLS
    at all, so a plain session works fine.  On Postgres, the ``api_keys_hash_lookup``
    PERMISSIVE policy created in migration 0002 allows SELECT when
    ``app.actor = 'APIKEY_LOOKUP'`` is set; ``get_raw_session`` does NOT set
    any context vars, so the per-user RLS policies would block the read —
    therefore a raw session is only safe when the migration's bypass policy is
    in place (which it is for Postgres; SQLite doesn't do RLS).
    """
    async for session in get_raw_session():
        stmt = select(ApiKey).where(
            ApiKey.key_hash == key_hash,
            ApiKey.revoked_at.is_(None),
        )
        result = await session.execute(stmt)
        record = result.scalar_one_or_none()

        if record is None:
            raise HTTPException(status_code=401, detail="Invalid or revoked API key")

        identity = ApiKeyIdentity(
            key_id=record.id,
            user_id=record.user_id,
            tenant_id=record.tenant_id,
        )

        # Best-effort last_used_at update — don't let this fail the auth call
        try:
            record.last_used_at = datetime.now(UTC)
            session.add(record)
        except Exception as exc:  # noqa: BLE001
            logger.debug("api_key_auth: last_used_at update failed: %s", exc)

    return identity
