"""HTTP transport for the audit POST.

A single module-level ``httpx.Client`` is created lazily on the first
POST so that importing this module is cheap (and safe in environments
that don't actually use the sink, e.g. unit tests for sibling
extensions).

The client can be overridden — both for tests (inject a
``MockTransport``) and for any future use case that wants different
timeouts — via :func:`set_client`.
"""

from __future__ import annotations

from typing import Any

import httpx

_DEFAULT_TIMEOUT = httpx.Timeout(2.0, connect=1.0)
_CLIENT: httpx.Client | None = None


def set_client(client: httpx.Client | None) -> None:
    """Override (or reset) the module-level client.

    Passing ``None`` reverts to lazy default-client construction on the
    next call.
    """
    global _CLIENT
    _CLIENT = client


def _client() -> httpx.Client:
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = httpx.Client(timeout=_DEFAULT_TIMEOUT)
    return _CLIENT


def audit_post(*, base_url: str, token: str, body: dict[str, Any]) -> httpx.Response:
    """Synchronously POST ``body`` to ``{base_url}/audit/v1/events``.

    Raises ``httpx.HTTPError`` (or its subclasses) on transport failure
    and ``httpx.HTTPStatusError`` on 4xx/5xx so callers can choose how
    to react.  ``__init__.install`` swallows both into a ``warning``.
    """
    if not base_url:
        raise httpx.InvalidURL("FOUNDATION_API_URL is empty")
    url = base_url.rstrip("/") + "/audit/v1/events"
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    response = _client().post(url, json=body, headers=headers)
    response.raise_for_status()
    return response


__all__ = ["audit_post", "set_client"]
