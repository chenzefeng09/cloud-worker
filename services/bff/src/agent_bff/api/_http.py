"""Shared HTTP helpers for BFF → foundation calls.

These helpers used to be inlined in both ``sessions.py`` and
``conversations.py``; centralising them avoids two copies drifting when
foundation adds a new header (e.g. tracing) or changes the auth scheme.
"""

from __future__ import annotations

from collections.abc import Iterable

__all__ = ["SUPER_ADMIN_DEFAULT_SCOPES", "foundation_headers"]


# Default scopes the BFF asserts for a super_admin actor when no explicit set
# is provided.  Today the only scope-gated downstream surface is the
# foundation-lifecycle storage admin (cross-tenant quota summary / patch),
# which requires ``storage.admin`` (read) and ``storage.quota.write`` (write).
# Keep this set conservative — every additional scope here grants reach to a
# new downstream surface for *every* super_admin call.
SUPER_ADMIN_DEFAULT_SCOPES: tuple[str, ...] = (
    "storage.admin",
    "storage.quota.write",
)


def foundation_headers(
    tenant_id: str,
    user_id: str,
    access_token: str,
    *,
    scopes: Iterable[str] | None = None,
    super_admin: bool = False,
    clearance: str | None = None,
) -> dict[str, str]:
    """Build the header set BFF forwards to every foundation call.

    ``X-Tenant-Id`` / ``X-User-Id`` are belt-and-braces alongside the
    Authorization bearer: when running behind a gateway that already
    extracts the JWT into headers they are redundant, but when running
    against a bare foundation service in dev they are how the tenant
    context arrives.

    ``scopes``: when set, attach as the space-separated ``X-Foundation-Scopes``
    header.  An explicit empty iterable suppresses the header.

    ``super_admin``: when True and ``scopes`` is not explicitly provided,
    auto-attach the default super_admin scope set
    (``SUPER_ADMIN_DEFAULT_SCOPES``).  This is the path used by endpoints
    behind ``require_super_admin_bff`` that touch scope-gated foundation
    surfaces (lifecycle storage admin today).  Passing an explicit ``scopes``
    argument always wins over the super_admin default.

    ``clearance``: when a non-empty string is passed, attach it as the
    ``X-User-Clearance`` header.  Used by the orchestrator DLP-download proxy
    so the downstream DLP guard can enforce the caller's clearance level.
    Omitted/empty → header is not sent (backward compatible for all other
    foundation proxies).
    """
    headers = {"X-Tenant-Id": tenant_id, "X-User-Id": user_id}
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
    if clearance:
        headers["X-User-Clearance"] = clearance

    if scopes is not None:
        scope_list = [s for s in scopes if s]
    elif super_admin:
        scope_list = list(SUPER_ADMIN_DEFAULT_SCOPES)
    else:
        scope_list = []

    if scope_list:
        headers["X-Foundation-Scopes"] = " ".join(scope_list)

    return headers
