"""HTTP transport for the tenant's MCP-server registry lookup."""

from __future__ import annotations

from typing import Any

import httpx

_DEFAULT_TIMEOUT = httpx.Timeout(5.0, connect=2.0)
_CLIENT: httpx.Client | None = None


def set_client(client: httpx.Client | None) -> None:
    """Override the module-level ``httpx.Client`` (for tests)."""
    global _CLIENT
    _CLIENT = client


def _client() -> httpx.Client:
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = httpx.Client(timeout=_DEFAULT_TIMEOUT)
    return _CLIENT


def fetch_mcp_servers(
    *,
    base_url: str,
    token: str,
    tenant_id: str,
    service_scopes: str = "",
) -> list[dict[str, Any]]:
    """GET ``{base_url}/internal/mcp-servers?tenant_id=...``.

    ``base_url`` should be the mcp-registry service base URL including the
    API version prefix, e.g. ``http://mcp-registry:8010/api/mcp/v1``.
    The caller (``mcp_tenant_injector.__init__``) resolves this from
    ``MCP_REGISTRY_URL`` env (preferred) or falls back to constructing
    it from ``FOUNDATION_API_URL``.

    ``service_scopes`` is the value for the ``X-Service-Scopes`` header that
    the internal endpoint requires.  In production the API gateway sets this
    header after verifying the Bearer JWT; in dev (no gateway) the scheduler
    injects it via the ``FOUNDATION_SERVICE_SCOPES`` env var so the sandbox
    can assert the scopes directly.  Pass the empty string to omit the header
    (will result in a 403 from the service).

    Returns the list under ``"items"`` / ``"servers"`` (or the top-level list
    if the response is already an array).  Raises ``httpx.HTTPError`` /
    ``HTTPStatusError`` on transport or 4xx/5xx failures so the
    caller can decide whether to fall through.
    """
    if not base_url:
        raise httpx.InvalidURL("MCP_REGISTRY_URL / FOUNDATION_API_URL is empty")
    url = base_url.rstrip("/") + "/internal/mcp-servers"
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if tenant_id:
        headers["X-Tenant-Id"] = tenant_id
    if service_scopes:
        headers["X-Service-Scopes"] = service_scopes
    response = _client().get(url, params={"tenant_id": tenant_id}, headers=headers)
    response.raise_for_status()
    body = response.json()
    if isinstance(body, list):
        return list(body)
    if isinstance(body, dict):
        servers = body.get("servers") or body.get("items") or []
        if isinstance(servers, list):
            return list(servers)
    return []


__all__ = ["fetch_mcp_servers", "set_client"]
