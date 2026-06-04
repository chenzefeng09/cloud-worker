"""BFF API-key management endpoints.

Prefix: /api-keys   (no /v1 nesting — the api_prefix already contains /api/bff/v1)

All endpoints require normal session-cookie authentication.  The caller's
user_id and tenant_id are taken from the signed session cookie — never from
query parameters.

Security contract
-----------------
- The raw key is returned ONCE at creation time, then discarded.  Only the
  SHA-256 hex digest is stored in the database.
- ``key_prefix`` (first 16 chars) is safe to display and never enough to
  brute-force.  It uses ``cw_live_`` prefix so users can identify them.
- Logs never include the raw key.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from agent_bff.api.api_key_auth import ApiKeyIdentity, require_api_key
from agent_bff.api.dependencies import require_auth
from agent_bff.domain.db import get_session
from agent_bff.domain.models import ApiKey

router = APIRouter(tags=["API Keys"])
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

_KEY_PREFIX_TEXT = "cw_live_"
_KEY_RANDOM_BYTES = 24   # 32 base64-url-safe chars of entropy


def _generate_raw_key() -> str:
    """Return a new random key like ``cw_live_<32 url-safe chars>``."""
    token = secrets.token_urlsafe(_KEY_RANDOM_BYTES)
    return f"{_KEY_PREFIX_TEXT}{token}"


def _hash_key(raw_key: str) -> str:
    """SHA-256 hex digest of the raw key."""
    return hashlib.sha256(raw_key.encode()).hexdigest()


def _prefix(raw_key: str) -> str:
    """Return the first 16 chars of the raw key (safe to display)."""
    return raw_key[:16]


# ─────────────────────────────────────────────────────────────────────────────
# Request / response schemas
# ─────────────────────────────────────────────────────────────────────────────

class CreateApiKeyRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=120, description="Human-readable label")


class ApiKeyListItem(BaseModel):
    """Returned by GET /api-keys — never includes raw key or hash."""

    id: str
    name: str
    key_prefix: str
    created_at: str
    last_used_at: str | None


class ApiKeyCreated(BaseModel):
    """Returned by POST /api-keys — includes the raw key ONCE."""

    id: str
    name: str
    key_prefix: str
    created_at: str
    # The raw key — only returned at creation, never again.
    key: str


# ─────────────────────────────────────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/api-keys", response_model=list[ApiKeyListItem])
async def list_api_keys(
    _auth: tuple = Depends(require_auth()),
) -> list[ApiKeyListItem]:
    """List the current user's active (non-revoked) API keys.

    Returns name, prefix, created_at, last_used_at.  Raw key and hash are
    never exposed.
    """
    tenant_id, user_id, _ = _auth

    async for session in get_session(tenant_id=tenant_id, user_id=user_id):
        stmt = (
            select(ApiKey)
            .where(
                ApiKey.user_id == user_id,
                ApiKey.revoked_at.is_(None),
            )
            .order_by(ApiKey.created_at.desc())
        )
        result = await session.execute(stmt)
        rows = result.scalars().all()

    return [
        ApiKeyListItem(
            id=row.id,
            name=row.name,
            key_prefix=row.key_prefix,
            created_at=row.created_at.isoformat(),
            last_used_at=row.last_used_at.isoformat() if row.last_used_at else None,
        )
        for row in rows
    ]


@router.post("/api-keys", response_model=ApiKeyCreated, status_code=201)
async def create_api_key(
    body: CreateApiKeyRequest,
    _auth: tuple = Depends(require_auth()),
) -> ApiKeyCreated:
    """Create a new API key.

    Returns the raw key EXACTLY ONCE.  It cannot be retrieved later.
    The DB only stores the SHA-256 hash + the display prefix.
    """
    tenant_id, user_id, _ = _auth

    raw_key = _generate_raw_key()
    key_hash = _hash_key(raw_key)
    key_prefix = _prefix(raw_key)

    now = datetime.now(UTC)

    async for session in get_session(tenant_id=tenant_id, user_id=user_id):
        record = ApiKey(
            user_id=user_id,
            tenant_id=tenant_id,
            name=body.name,
            key_prefix=key_prefix,
            key_hash=key_hash,
            created_at=now,
        )
        session.add(record)
        await session.flush()  # get the generated id before the session closes
        new_id = record.id
        new_created_at = record.created_at

    logger.info(
        "api_key.created",
        extra={"user_id": user_id, "tenant_id": tenant_id, "key_id": new_id},
    )

    return ApiKeyCreated(
        id=new_id,
        name=body.name,
        key_prefix=key_prefix,
        created_at=new_created_at.isoformat(),
        key=raw_key,  # ONE-TIME only
    )


@router.delete("/api-keys/{key_id}", status_code=204, response_model=None)
async def revoke_api_key(
    key_id: str,
    _auth: tuple = Depends(require_auth()),
) -> None:
    """Soft-revoke an API key (sets revoked_at).

    Only the owning user can revoke their own keys.  Attempting to revoke a
    key belonging to another user returns 404 (not 403, to avoid leaking
    existence information).
    """
    tenant_id, user_id, _ = _auth

    async for session in get_session(tenant_id=tenant_id, user_id=user_id):
        stmt = select(ApiKey).where(
            ApiKey.id == key_id,
            ApiKey.user_id == user_id,
            ApiKey.revoked_at.is_(None),
        )
        result = await session.execute(stmt)
        record = result.scalar_one_or_none()

        if record is None:
            raise HTTPException(status_code=404, detail="API key not found or already revoked")

        record.revoked_at = datetime.now(UTC)
        session.add(record)

    logger.info(
        "api_key.revoked",
        extra={"user_id": user_id, "tenant_id": tenant_id, "key_id": key_id},
    )


# ─────────────────────────────────────────────────────────────────────────────
# Probe endpoint — protected by API key auth (for testing the middleware)
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/api-keys/probe")
async def probe_api_key_auth(
    identity: ApiKeyIdentity = Depends(require_api_key()),
) -> dict:
    """Test/probe endpoint: authenticate with an API key and return the resolved identity.

    This endpoint is protected by ``require_api_key()`` (NOT the session cookie),
    which makes it useful for verifying that the API key auth middleware works.

    Returns:
        {"key_id": ..., "user_id": ..., "tenant_id": ...}
    """
    return {
        "key_id": identity.key_id,
        "user_id": identity.user_id,
        "tenant_id": identity.tenant_id,
    }
