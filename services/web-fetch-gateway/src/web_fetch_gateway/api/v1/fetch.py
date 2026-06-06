"""POST /fetch — the main gateway endpoint."""

from __future__ import annotations

from fastapi import APIRouter, Request

from web_fetch_gateway import fetcher
from web_fetch_gateway.config import Settings
from web_fetch_gateway.errors import FetchAuthError
from web_fetch_gateway.schemas import FetchRequest, FetchResponse

router = APIRouter(tags=["Fetch"])


def _require_auth_context(request: Request, settings: Settings) -> None:
    if not settings.require_auth:
        return

    auth = request.headers.get("Authorization", "")
    tenant_id = request.headers.get("X-Tenant-Id", "")
    user_id = request.headers.get("X-User-Id", "")
    scopes_raw = request.headers.get("X-Service-Scopes", "")
    scopes = {s.strip() for s in scopes_raw.split(",") if s.strip()}

    if not auth.startswith("Bearer ") or not auth.removeprefix("Bearer ").strip():
        raise FetchAuthError("Authorization bearer token required")
    if not tenant_id:
        raise FetchAuthError("X-Tenant-Id header required")
    if not user_id:
        raise FetchAuthError("X-User-Id header required")
    if settings.required_scope and settings.required_scope not in scopes:
        raise FetchAuthError(
            f"Required service scope {settings.required_scope!r} missing",
            status=403,
        )


@router.post("/fetch", response_model=FetchResponse)
async def fetch_url(body: FetchRequest, request: Request) -> FetchResponse:
    """Fetch a remote URL with SSRF protection.

    Security: all requests pass through the full check chain (scheme →
    domain allowlist → IP blocklist) before any network I/O is initiated.
    Redirects are followed manually with security re-validation at each hop.

    Authentication is fail-closed by default: callers must forward the
    scheduler-issued bearer token, tenant/user context, and ``web_fetch.invoke``
    service scope.
    """
    settings: Settings = request.app.state.settings
    _require_auth_context(request, settings)

    # Per-request byte cap, bounded to the server-side maximum.
    max_bytes = min(
        body.max_bytes if body.max_bytes is not None else settings.max_response_bytes,
        settings.max_response_bytes,
    )

    result = await fetcher.fetch(
        body.url,
        method=body.method,
        headers=body.headers,
        settings=settings,
        max_bytes=max_bytes,
    )

    return FetchResponse(
        url=body.url,
        status_code=result.status_code,
        content_type=result.content_type,
        body=result.body,
        truncated=result.truncated,
    )
