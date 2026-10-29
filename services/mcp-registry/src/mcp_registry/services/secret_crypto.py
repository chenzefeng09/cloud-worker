"""Envelope encryption for MCP server secrets.

Secret fields inside an ``auth`` JSONB blob (``token`` for bearer,
``api_key`` for api_key, ``client_secret`` for oauth), and every value of the
``env`` and ``headers`` maps (API keys as env vars, ``Authorization`` headers),
are encrypted at rest via the foundation crypto service and decrypted on read
so that:

  * the DB columns never hold plaintext secrets,
  * the admin surface shows ``env`` / ``headers`` in plaintext (auth secrets
    are masked, see below) and the internal consumer endpoint returns
    everything in plaintext,
  * consumers (``mcp_tenant_injector``) never have to know about envelopes.

The contract mirrors ``llm-gateway``'s ``_encrypt_api_key`` / ``_decrypt_api_key``
helpers (``/api/crypto/v1/encrypt`` + ``/decrypt`` with ``key_label`` /
``plaintext_b64`` / ``envelope``).  We use raw ``httpx`` rather than the
``foundation_sdk`` CryptoResource because the SDK resource is out of sync with
the live crypto service schema (it sends ``kek_label`` / ``plaintext`` instead
of ``key_label`` / ``plaintext_b64``); llm-gateway is the verified reference.

Resilience
----------
  * ``encrypt_secret`` fails closed: when the crypto service can't encrypt a
    value it raises ``SecretEncryptionUnavailableError`` (HTTP 503), so a
    secret is never written in clear.  ``allow_plaintext=True`` (the
    ``allow_plaintext_secrets`` setting, local development only) restores the
    old plaintext fallback.  It is idempotent: a value that already looks like
    an envelope is returned unchanged (so a re-encrypt on update never
    double-wraps).
  * ``decrypt_secret`` never raises.  It treats a non-envelope value as legacy
    plaintext and
    returns it as-is (migration compatibility).  A failed decrypt falls back
    to the stored value - worst case the upstream call 401s, which is
    preferable to crashing the registry.

Masking
-------
``mask_auth`` replaces secret fields with a sentinel (``MASKED_SENTINEL``)
for the tenant-facing admin surface so plaintext credentials never reach the
browser.  Each masked field gets a ``has_{field}: True`` sibling so the UI
knows a secret is stored even though the value is redacted.  The internal
consumer endpoint (``/internal/mcp-servers``) bypasses masking and returns
plaintext as before.

The same caveat as llm-gateway applies in dev: the Vault transit backend is
ephemeral and a reset invalidates existing envelopes.  Re-create the affected
MCP servers (or re-run the backfill script) when that happens.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from mcp_registry.errors import SecretEncryptionUnavailableError

log = logging.getLogger(__name__)

# Sentinel value used by mask_auth to replace plaintext secrets on the
# tenant-facing admin surface.  Exported as MASKED_SENTINEL for the PATCH
# handler's "unchanged" detection and for schema validation.
MASKED_SENTINEL = "••••••"  # "••••••"


# Fields that carry a plaintext secret per auth type.  Other fields
# (``header_name`` / ``client_id`` / ``token_url`` / ``scopes``) are not
# sensitive and are stored as-is.
_SECRET_FIELDS: dict[str, tuple[str, ...]] = {
    "bearer": ("token",),
    "api_key": ("api_key",),
    "oauth": ("client_secret",),
}


def is_envelope(value: Any) -> bool:
    """Return True if ``value`` looks like a crypto envelope JSON string.

    The crypto service's envelope always carries ``ciphertext_b64``; we use
    that as the sentinel.  Non-string / non-JSON / non-envelope values are
    treated as plaintext (legacy or post-fallback).
    """
    if not isinstance(value, str):
        return False
    try:
        obj = json.loads(value)
    except (json.JSONDecodeError, ValueError):
        return False
    return isinstance(obj, dict) and "ciphertext_b64" in obj


async def encrypt_secret(
    plaintext: str,
    *,
    crypto_url: str,
    timeout: float,
    tenant_id: str,
    allow_plaintext: bool = False,
) -> str:
    """Envelope-encrypt ``plaintext`` via the foundation crypto service.

    Returns the envelope JSON string for storage.  Idempotent: an already
    encrypted value is returned unchanged.  On any crypto failure (logged at
    WARNING) it raises ``SecretEncryptionUnavailableError``, or returns the
    plaintext when ``allow_plaintext`` is set.
    """
    if not plaintext:
        return plaintext
    if is_envelope(plaintext):
        # Already encrypted (e.g. admin re-submits a previously-encrypted
        # value) - never double-wrap.
        return plaintext
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                f"{crypto_url}/encrypt",
                headers={"X-Tenant-Id": tenant_id, "X-Actor": "SERVICE"},
                json={
                    "key_label": "default",
                    "plaintext_b64": base64.b64encode(plaintext.encode()).decode(),
                },
            )
        if resp.status_code == 200:
            envelope = resp.json().get("envelope")
            if isinstance(envelope, dict):
                return json.dumps(envelope)
        log.warning(
            "mcp.crypto.encrypt_non_200",
            extra={"status": resp.status_code, "body": resp.text[:300]},
        )
    except Exception as exc:  # noqa: BLE001 - every failure takes the same exit
        log.warning("mcp.crypto.encrypt_failed", extra={"error": str(exc)})
    if allow_plaintext:
        return plaintext
    raise SecretEncryptionUnavailableError


async def decrypt_secret(
    stored: str,
    *,
    crypto_url: str,
    timeout: float,
    tenant_id: str,
) -> str:
    """Decrypt a stored secret envelope back to plaintext.

    Migration-compatible: a non-envelope value (legacy plaintext or a
    post-fallback plaintext) is returned as-is.  A failed decrypt falls back
    to the stored value - the upstream call may 401, but the registry stays up.
    """
    if not stored or not is_envelope(stored):
        return stored
    try:
        envelope = json.loads(stored)
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                f"{crypto_url}/decrypt",
                headers={"X-Tenant-Id": tenant_id, "X-Actor": "SERVICE"},
                json={"envelope": envelope},
            )
        if resp.status_code == 200:
            b64 = resp.json().get("plaintext_b64")
            if b64:
                return base64.b64decode(b64).decode()
        log.warning(
            "mcp.crypto.decrypt_non_200",
            extra={"status": resp.status_code, "body": resp.text[:300]},
        )
    except Exception as exc:  # noqa: BLE001 - decrypt must never crash reads
        log.warning("mcp.crypto.decrypt_failed", extra={"error": str(exc)})
    return stored


def _secret_fields_for(auth: dict[str, Any]) -> tuple[str, ...]:
    """Return the secret field names that apply to ``auth["type"]``."""
    auth_type = str(auth.get("type") or "none").lower()
    return _SECRET_FIELDS.get(auth_type, ())


def has_secret(auth: dict[str, Any] | None) -> bool:
    """Return True if ``auth`` actually carries secret material.

    "Carries a secret" means: the auth type is one that has secret fields
    (bearer / api_key / oauth) *and* the corresponding field holds a non-empty
    string.  An ``auth`` that is ``None`` / empty / ``type: none``, or one whose
    secret field is blank, carries nothing worth protecting.

    Callers use this to decide whether a request would leak stored credential
    material - notably ``POST /mcp-servers/{id}/test``, which must refuse to
    decrypt-and-send a stored secret to a caller-redirected endpoint.  It is a
    pure predicate over the *stored* shape (envelope or legacy plaintext both
    read as "has a secret"), so it never touches the crypto service.
    """
    if not auth:
        return False
    return any(
        isinstance(auth.get(field), str) and auth.get(field)
        for field in _secret_fields_for(auth)
    )


async def encrypt_auth(
    auth: dict[str, Any] | None,
    *,
    crypto_url: str,
    timeout: float,
    tenant_id: str,
    allow_plaintext: bool = False,
) -> dict[str, Any] | None:
    """Return a copy of ``auth`` with its secret fields envelope-encrypted.

    ``None`` / empty auth passes through unchanged.  Non-secret fields
    (``header_name`` / ``client_id`` / ``token_url`` / ``scopes``) are copied
    as-is.
    """
    if not auth:
        return auth
    out = dict(auth)
    for field in _secret_fields_for(auth):
        value = out.get(field)
        if isinstance(value, str) and value:
            out[field] = await encrypt_secret(
                value, crypto_url=crypto_url, timeout=timeout, tenant_id=tenant_id,
                allow_plaintext=allow_plaintext,
            )
    return out


async def decrypt_auth(
    auth: dict[str, Any] | None,
    *,
    crypto_url: str,
    timeout: float,
    tenant_id: str,
) -> dict[str, Any] | None:
    """Return a copy of ``auth`` with its secret fields decrypted to plaintext.

    ``None`` / empty auth passes through unchanged.  Safe to call on any auth
    shape (encrypted, legacy plaintext, or ``none``).
    """
    if not auth:
        return auth
    out = dict(auth)
    for field in _secret_fields_for(auth):
        value = out.get(field)
        if isinstance(value, str) and value:
            out[field] = await decrypt_secret(
                value, crypto_url=crypto_url, timeout=timeout, tenant_id=tenant_id,
            )
    return out


async def _map_values(
    values: dict[str, Any] | None, transform: Callable[[str], Awaitable[str]],
) -> dict[str, Any]:
    """Apply ``transform`` to every non-empty string value, concurrently."""
    if not values:
        return dict(values or {})
    keys = [k for k, v in values.items() if isinstance(v, str) and v]
    done = await asyncio.gather(*(transform(values[k]) for k in keys))
    return {**values, **dict(zip(keys, done, strict=True))}


async def encrypt_values(
    values: dict[str, Any] | None,
    *,
    crypto_url: str,
    timeout: float,
    tenant_id: str,
    allow_plaintext: bool = False,
) -> dict[str, Any]:
    """Envelope-encrypt every value of an ``env`` / ``headers`` map (keys stay)."""
    return await _map_values(values, lambda value: encrypt_secret(
        value, crypto_url=crypto_url, timeout=timeout, tenant_id=tenant_id,
        allow_plaintext=allow_plaintext,
    ))


async def decrypt_values(
    values: dict[str, Any] | None,
    *,
    crypto_url: str,
    timeout: float,
    tenant_id: str,
) -> dict[str, Any]:
    """Inverse of ``encrypt_values``; legacy plaintext values pass through."""
    return await _map_values(values, lambda value: decrypt_secret(
        value, crypto_url=crypto_url, timeout=timeout, tenant_id=tenant_id,
    ))


def mask_auth(auth: dict[str, Any] | None) -> dict[str, Any] | None:
    """Replace secret fields in ``auth`` with ``MASKED_SENTINEL``.

    Pure function (no crypto calls).  Returns ``auth`` unchanged when it is
    ``None`` / empty or ``type`` is ``none``.

    For each secret field defined in ``_SECRET_FIELDS[auth["type"]]``:

      * If the field holds a truthy string, replace it with ``MASKED_SENTINEL``
        and add a sibling ``has_{field}: True`` so the UI can indicate that a
        secret is stored.
      * Otherwise set ``has_{field}: False`` and leave the field as-is.

    Non-secret fields (``header_name``, ``client_id``, ``token_url``,
    ``scopes``) pass through unchanged.
    """
    if not auth:
        return auth
    auth_type = str(auth.get("type") or "none").lower()
    if auth_type == "none":
        return auth
    out = dict(auth)
    for field in _secret_fields_for(auth):
        value = out.get(field)
        if isinstance(value, str) and value:
            out[field] = MASKED_SENTINEL
            out[f"has_{field}"] = True
        else:
            out[f"has_{field}"] = False
    return out
