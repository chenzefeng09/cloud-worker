"""Shared HTTP helpers for BFF → foundation calls.

These helpers used to be inlined in both ``sessions.py`` and
``conversations.py``; centralising them avoids two copies drifting when
foundation adds a new header (e.g. tracing) or changes the auth scheme.
"""

from __future__ import annotations

__all__ = ["foundation_headers"]


def foundation_headers(
    tenant_id: str, user_id: str, access_token: str,
) -> dict[str, str]:
    """Build the header set BFF forwards to every foundation call.

    ``X-Tenant-Id`` / ``X-User-Id`` are belt-and-braces alongside the
    Authorization bearer: when running behind a gateway that already
    extracts the JWT into headers they are redundant, but when running
    against a bare foundation service in dev they are how the tenant
    context arrives.
    """
    headers = {"X-Tenant-Id": tenant_id, "X-User-Id": user_id}
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
    return headers
