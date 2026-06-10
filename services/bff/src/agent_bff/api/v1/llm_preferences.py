"""BFF LLM user-preferences — proxy to llm-gateway /user-preferences.

Prefix: /llm

All endpoints require normal authentication (session cookie).  The caller's
tenant_id and user_id are derived from the signed session cookie — they are
NOT accepted as query parameters, preventing horizontal privilege escalation.
"""

from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ValidationError, field_validator

from agent_bff.api._http import foundation_headers
from agent_bff.api.dependencies import require_auth

router = APIRouter(tags=["LLM Preferences"])
logger = logging.getLogger(__name__)

# Allowed LLM purposes — kept in sync with the gateway's enum.
_ALLOWED_PURPOSES = frozenset({"llm", "embedding", "rerank", "tts", "stt"})


def _validate_purpose(purpose: str) -> str:
    """Reject any purpose outside the gateway's enum with 422."""
    if purpose not in _ALLOWED_PURPOSES:
        raise HTTPException(
            status_code=422,
            detail=f"purpose must be one of {sorted(_ALLOWED_PURPOSES)}",
        )
    return purpose


class UserPreferenceRequest(BaseModel):
    """Minimal validation for PUT /llm/user-preferences.

    The gateway owns the full field set; the BFF only guards the three required
    identifiers (and the purpose enum) so a malformed write fails fast.
    """

    model_config = {"extra": "allow"}

    purpose: str
    model_id: str
    provider_id: str

    @field_validator("purpose")
    @classmethod
    def _check_purpose(cls, v: str) -> str:
        if v not in _ALLOWED_PURPOSES:
            raise ValueError(f"purpose must be one of {sorted(_ALLOWED_PURPOSES)}")
        return v

    @field_validator("model_id", "provider_id")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("must be a non-empty string")
        return v


# ─────────────────────────────────────────────────────────────────────────────
# Model catalogue — sanitized proxy to llm-gateway /providers
# ─────────────────────────────────────────────────────────────────────────────

# Fields safe to expose to non-admin users for model selection. Notably this
# EXCLUDES base_url, api_key (and any credential/internal config) — only the
# catalogue metadata a user needs to pick a model is surfaced.
_SAFE_PROVIDER_FIELDS = ("id", "name", "kind", "enabled", "models")
# Capability sub-keys safe to expose: the type map (model→category) and the
# per-model settings (context_length, max_tokens, vision flags) used for
# selection. Anything else under capabilities is dropped.
_SAFE_CAPABILITY_KEYS = ("model_type_map", "model_settings")


def _sanitize_provider(provider: dict) -> dict:
    """Strip a gateway provider down to user-safe catalogue fields.

    Drops base_url, credentials and any other internal config; keeps only the
    whitelisted top-level fields plus a filtered `capabilities` subset.
    """
    safe = {k: provider.get(k) for k in _SAFE_PROVIDER_FIELDS}
    caps = provider.get("capabilities")
    safe_caps: dict = {}
    if isinstance(caps, dict):
        for key in _SAFE_CAPABILITY_KEYS:
            if key in caps:
                safe_caps[key] = caps[key]
    safe["capabilities"] = safe_caps
    return safe


@router.get("/llm/models")
async def list_models(
    request: Request,
    _auth: tuple = Depends(require_auth()),
) -> dict:
    """List enabled LLM providers (sanitized) for model selection.

    Unlike the admin /admin/llm/providers endpoint, this requires only normal
    authentication. The response is sanitized — credentials, base_url and other
    internal provider config are never exposed — and only enabled providers are
    returned.
    """
    tenant_id, user_id, access_token = _auth
    settings = request.app.state.settings
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{settings.foundation_llm_gateway_url}/providers",
            headers=foundation_headers(tenant_id, user_id, access_token),
        )
    _raise_for_upstream(resp)
    payload = resp.json()
    raw_items = payload.get("items", []) if isinstance(payload, dict) else []
    items = [
        _sanitize_provider(p)
        for p in raw_items
        if isinstance(p, dict) and p.get("enabled")
    ]
    return {"items": items}


# ─────────────────────────────────────────────────────────────────────────────
# User preferences — proxy to llm-gateway /user-preferences
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/llm/user-preferences")
async def get_user_preferences(
    request: Request,
    _auth: tuple = Depends(require_auth()),
) -> dict:
    """Return the authenticated user's LLM preferences."""
    tenant_id, user_id, access_token = _auth
    settings = request.app.state.settings
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{settings.foundation_llm_gateway_url}/user-preferences",
            headers=foundation_headers(tenant_id, user_id, access_token),
        )
    _raise_for_upstream(resp)
    return resp.json()


@router.put("/llm/user-preferences")
async def update_user_preferences(
    request: Request,
    _auth: tuple = Depends(require_auth()),
) -> dict:
    """Create or replace the authenticated user's LLM preferences."""
    tenant_id, user_id, access_token = _auth
    settings = request.app.state.settings
    raw = await request.json() if await request.body() else {}
    try:
        model = UserPreferenceRequest.model_validate(raw)
    except ValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail=exc.errors(include_url=False, include_context=False),
        ) from exc
    body = model.model_dump()
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.put(
            f"{settings.foundation_llm_gateway_url}/user-preferences",
            json=body,
            headers=foundation_headers(tenant_id, user_id, access_token),
        )
    _raise_for_upstream(resp)
    return resp.json()


@router.get("/llm/user-preferences/resolve")
async def resolve_user_preferences(
    request: Request,
    _auth: tuple = Depends(require_auth()),
) -> dict:
    """Resolve the effective LLM preferences for the authenticated user."""
    tenant_id, user_id, access_token = _auth
    settings = request.app.state.settings
    params = dict(request.query_params)
    if "purpose" in params:
        _validate_purpose(params["purpose"])
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{settings.foundation_llm_gateway_url}/user-preferences/resolve",
            params=params,
            headers=foundation_headers(tenant_id, user_id, access_token),
        )
    _raise_for_upstream(resp)
    return resp.json()


@router.delete("/llm/user-preferences/{purpose}")
async def delete_user_preference(
    purpose: str,
    request: Request,
    _auth: tuple = Depends(require_auth()),
) -> dict:
    """Delete the authenticated user's LLM preference for a given purpose."""
    _validate_purpose(purpose)
    tenant_id, user_id, access_token = _auth
    settings = request.app.state.settings
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.delete(
            f"{settings.foundation_llm_gateway_url}/user-preferences/{purpose}",
            headers=foundation_headers(tenant_id, user_id, access_token),
        )
    _raise_for_upstream(resp)
    return {"status": "deleted"}


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _raise_for_upstream(resp: httpx.Response) -> None:
    """Surface upstream 4xx/5xx as HTTPException to the SPA."""
    if resp.status_code >= 400:
        try:
            detail = resp.json()
        except Exception:
            detail = resp.text or "upstream error"
        raise HTTPException(status_code=resp.status_code, detail=detail)
