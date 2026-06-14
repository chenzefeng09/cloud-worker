"""WebFetch tool — lets the in-sandbox agent fetch public HTTP/HTTPS pages.

The tool talks to ``web-fetch-gateway`` (a sidecar running inside the
``foundation`` network) via a synchronous ``httpx.POST``.  The gateway owns
domain-allow-listing, private-range blocking, content-type restrictions, and
response-size caps; this class is just the adapter between AgentScope's tool
protocol and the gateway's HTTP API.

AgentScope 2.x tool contract
-----------------------------
The ``_workspace_tools`` module shows that built-in tools are registered by
appending **instances** of tool classes to ``toolkit.tool_groups[0].tools``.
AgentScope discovers each tool's name and schema from the instance.  The
actual mechanism (as observed in the agentscope source tree that ships with
the sandbox image) is:

* AgentScope reads ``instance.name`` for the tool name exposed to the LLM.
* AgentScope reads ``instance.__doc__`` (the class docstring) for the tool
  description it sends to the LLM.
* AgentScope reads ``instance.__call__``'s signature (via inspection) to
  build the JSON-schema for the tool parameters.

No base class or decorator is required — a plain Python class whose instance
is callable satisfies the contract.  ``Read``, ``Grep``, and ``Glob`` in
agentscope.tool all follow this same pattern.  We follow it here.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# AgentScope 2.x base-class resolution.
#
# The agent toolkit builds each tool's JSON schema from ``ToolBase`` instance
# attributes (``name`` / ``description`` / ``input_schema``) and invokes the
# abstract ``check_permissions`` during dispatch.  A plain class is rejected
# with ``AttributeError: 'WebFetchTool' object has no attribute
# 'input_schema'`` the moment the agent tries to expose its tools to the LLM.
#
# We resolve ``ToolBase`` (and the permission/response helpers) at import time
# but degrade gracefully to a plain ``object`` base when agentscope is absent
# (unit tests / lint), so the module stays importable everywhere.
# ---------------------------------------------------------------------------
try:  # pragma: no cover - exercised in the sandbox image, stubbed in tests
    from agentscope.tool import ToolBase as _ToolBase
    from agentscope.tool._response import ToolChunk as _ToolChunk
    from agentscope.message import TextBlock as _TextBlock, ToolResultState as _ToolResultState
    from agentscope.permission import (
        PermissionBehavior as _PermissionBehavior,
        PermissionContext as _PermissionContext,
        PermissionDecision as _PermissionDecision,
    )

    _AGENTSCOPE_TOOLS = True
except Exception:  # noqa: BLE001 - keep importable without agentscope
    _ToolBase = object  # type: ignore[assignment,misc]
    _ToolChunk = None  # type: ignore[assignment]
    _TextBlock = None  # type: ignore[assignment]
    _ToolResultState = None  # type: ignore[assignment]
    _PermissionBehavior = None  # type: ignore[assignment]
    _PermissionContext = Any  # type: ignore[assignment]
    _PermissionDecision = None  # type: ignore[assignment]
    _AGENTSCOPE_TOOLS = False


class WebFetchTool(_ToolBase):
    """Fetch a public HTTP/HTTPS URL and return its content as text.

    Use this tool to retrieve web pages, public APIs, documentation, or any
    other resource reachable via HTTP/HTTPS.  The request is proxied through
    the platform's ``web-fetch-gateway``, which enforces:

    * **Domain allow-listing** — only domains listed in
      ``WEB_FETCH_ALLOWED_DOMAINS`` (or all public domains when set to ``*``)
      are reachable.
    * **Private-range blocking** — RFC-1918 addresses, loopback, and link-local
      ranges are always rejected, regardless of the allow-list.
    * **Content-type restrictions** — when enabled, only text-like content
      types are returned; binary blobs (images, executables, …) are rejected.

    The response includes the final URL (after any redirects), HTTP status
    code, Content-Type header, and the response body (which may be truncated
    if it exceeds the configured byte cap).  A note is appended when the body
    was truncated.

    Args:
        url: The full HTTP or HTTPS URL to fetch.  Must include the scheme
            (``http://`` or ``https://``).
        max_bytes: Maximum response body size in bytes.  Defaults to 1 MiB
            (1048576).  The gateway may impose its own lower cap regardless
            of this value.

    Returns:
        A plain-text string beginning with ``[WebFetch]`` on success, or
        ``[WebFetch error]`` / ``[WebFetch blocked]`` on failure.  The caller
        (the LLM) should parse this text rather than expecting structured JSON.
    """

    name = "WebFetch"

    # ``ToolBase`` contract — consumed by the toolkit when it builds the JSON
    # schema sent to the LLM and when it dispatches the call.
    description: str = (
        "Fetch a public HTTP/HTTPS URL and return its content as text. "
        "Use this to retrieve web pages, public APIs, or online documentation. "
        "Requests are proxied through the platform web-fetch gateway, which "
        "enforces domain allow-listing, private-range blocking, and size caps. "
        "The result is a plain-text string beginning with '[WebFetch]' on "
        "success or '[WebFetch error]'/'[WebFetch blocked]' on failure."
    )

    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": (
                    "The full HTTP or HTTPS URL to fetch. Must include the "
                    "scheme (http:// or https://)."
                ),
            },
            "max_bytes": {
                "type": "integer",
                "description": (
                    "Maximum response body size in bytes (default 1048576). "
                    "The gateway may impose a lower cap."
                ),
                "default": 1048576,
                "minimum": 1,
            },
        },
        "required": ["url"],
    }

    is_mcp: bool = False
    is_read_only: bool = True
    is_concurrency_safe: bool = True
    is_external_tool: bool = False
    is_state_injected: bool = False

    def __init__(
        self,
        gateway_url: str,
        *,
        token: str = "",
        tenant_id: str = "",
        user_id: str = "",
        service_scopes: str = "",
        broker_lease: str = "",
    ) -> None:
        # Strip any trailing slash so we can safely append the path.
        self._gateway_url = gateway_url.rstrip("/")
        self._token = token
        self._tenant_id = tenant_id
        self._user_id = user_id
        self._service_scopes = service_scopes
        self._broker_lease = broker_lease

    async def check_permissions(
        self,
        tool_input: dict[str, Any],
        context: Any,
    ) -> Any:
        """WebFetch is read-only and proxied through the policy-enforcing
        gateway, so we let the engine continue with its normal rule matching.
        """
        return _PermissionDecision(
            behavior=_PermissionBehavior.PASSTHROUGH,
            message="WebFetch is read-only and gateway-policed.",
        )

    def __call__(self, url: str, max_bytes: Optional[int] = 1048576) -> Any:
        """Fetch ``url`` and return an AgentScope ``ToolChunk``."""
        text = self._fetch_text(url, max_bytes)
        if _AGENTSCOPE_TOOLS and _ToolChunk is not None:
            return _ToolChunk(
                content=[_TextBlock(type="text", text=text)],
                state=_ToolResultState.SUCCESS,
                is_last=True,
            )
        return text

    def _fetch_text(self, url: str, max_bytes: Optional[int] = 1048576) -> str:
        """Fetch ``url`` and return the response as text.

        Args:
            url: Full HTTP/HTTPS URL to fetch.
            max_bytes: Maximum response body bytes (default 1 MiB).

        Returns:
            Agent-readable text with the fetch result or an error message.
        """
        import httpx

        if self._gateway_url.endswith("/api/web-fetch/v1"):
            endpoint = f"{self._gateway_url}/fetch"
        else:
            endpoint = f"{self._gateway_url}/api/web-fetch/v1/fetch"
        payload: dict = {"url": url, "max_bytes": max_bytes or 1048576}
        headers: dict[str, str] = {}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        if self._tenant_id:
            headers["X-Tenant-Id"] = self._tenant_id
        if self._user_id:
            headers["X-User-Id"] = self._user_id
        if self._service_scopes:
            headers["X-Service-Scopes"] = self._service_scopes
        if self._broker_lease:
            headers["X-Tool-Broker-Lease"] = self._broker_lease

        try:
            resp = httpx.post(endpoint, json=payload, headers=headers, timeout=30.0)
        except httpx.TimeoutException:
            return f"[WebFetch error] Request to {url!r} timed out (30 s)"
        except httpx.ConnectError as exc:
            return f"[WebFetch error] Could not reach gateway at {self._gateway_url!r}: {exc}"
        except Exception as exc:  # noqa: BLE001
            return f"[WebFetch error] Unexpected error contacting gateway: {exc}"

        # ---- gateway error codes -----------------------------------------
        if resp.status_code == 403:
            try:
                detail = resp.json().get("detail", resp.text[:200])
            except Exception:  # noqa: BLE001
                detail = resp.text[:200]
            return f"[WebFetch blocked] {url}: {detail}"

        if resp.status_code == 504:
            return (
                f"[WebFetch error] Gateway timed out while fetching {url!r}. "
                "The remote server did not respond in time."
            )

        if resp.status_code >= 400:
            return f"[WebFetch error] Gateway returned HTTP {resp.status_code} for {url!r}"

        # ---- success -------------------------------------------------------
        try:
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            return f"[WebFetch error] Could not parse gateway response: {exc}"

        if data.get("error"):
            return f"[WebFetch error] {url}: {data['error']}"

        status_code = data.get("status_code", "?")
        content_type = data.get("content_type", "?")
        body = data.get("body", "")
        truncated = data.get("truncated", False)

        result = (
            f"[WebFetch] {url} — HTTP {status_code} ({content_type})\n\n{body}"
        )
        if truncated:
            result += "\n[... response truncated at limit ...]"
        return result
