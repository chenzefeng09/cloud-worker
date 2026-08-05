"""MCP server schemas."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Transport(StrEnum):
    HTTP = "http"
    SSE = "sse"
    STDIO = "stdio"


class AuthType(StrEnum):
    NONE = "none"
    BEARER = "bearer"
    API_KEY = "api_key"
    OAUTH = "oauth"


class McpAuthConfig(BaseModel):
    """Auth configuration for an MCP server.

    Phase 2 supports ``none`` / ``bearer`` / ``api_key`` / ``oauth``.

    Secret fields (``token`` / ``api_key`` / ``client_secret``) arrive as
    plaintext on write and are envelope-encrypted by the API layer before
    storage (see ``services/secret_crypto.py``); read paths decrypt them back
    to plaintext.  The schema itself only validates shape - encryption is
    transport-level, not model-level.
    """

    type: AuthType = AuthType.NONE
    token: str | None = None
    header_name: str | None = None
    api_key: str | None = None
    # OAuth 2.0 client-credentials flow (Phase 2).  ``client_secret`` is a
    # secret field - encrypted at rest like ``token`` / ``api_key``.
    # ``scopes`` is Optional so it is dropped by ``model_dump(exclude_none=...)``
    # for non-oauth types and for oauth servers that omit it (``scopes?``).
    client_id: str | None = None
    client_secret: str | None = None
    token_url: str | None = None
    scopes: list[str] | None = None

    @model_validator(mode="after")
    def _check_required_fields(self) -> McpAuthConfig:
        if self.type == AuthType.BEARER and not self.token:
            raise ValueError("auth.token is required when auth.type is bearer")
        if self.type == AuthType.API_KEY:
            if not self.api_key:
                raise ValueError("auth.api_key is required when auth.type is api_key")
            if not self.header_name:
                self.header_name = "X-API-Key"
        if self.type == AuthType.OAUTH:
            missing = [
                name
                for name, val in (
                    ("client_id", self.client_id),
                    ("client_secret", self.client_secret),
                    ("token_url", self.token_url),
                )
                if not val
            ]
            if missing:
                raise ValueError(
                    "auth." + ", auth.".join(missing)
                    + " required when auth.type is oauth"
                )
        return self

    @model_validator(mode="before")
    @classmethod
    def _treat_missing_type_as_none(cls, data: Any) -> Any:
        # A bare ``null`` or ``{}`` auth payload means "no auth".
        if isinstance(data, dict) and not data.get("type"):
            data = {**data, "type": AuthType.NONE}
        return data


class McpToolFilter(BaseModel):
    allow: list[str] = Field(default_factory=list)
    block: list[str] = Field(default_factory=list)


class McpTestTool(BaseModel):
    """Shape of a single discovered MCP tool.

    Persisted in ``mcp_servers.discovered_tools`` (migration 0004) and
    returned by the ``/probe`` and ``/test`` endpoints in ``McpTestResult``.
    Only ``name`` and ``description`` are captured - extra fields the upstream
    returns (e.g. ``inputSchema``) are dropped by the tester before
    persistence so the stored shape stays stable across MCP protocol revisions.
    """

    name: str
    description: str | None = None


class McpServerCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    transport: Transport
    endpoint: str | None = Field(None, max_length=500)
    command: str | None = Field(None, max_length=500)
    env: dict[str, str] = Field(default_factory=dict)
    enabled: bool = True
    headers: dict[str, str] = Field(default_factory=dict)
    auth: McpAuthConfig | None = None
    timeout_seconds: int | None = Field(None, ge=1)
    tool_filter: McpToolFilter = Field(default_factory=McpToolFilter)
    args: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_endpoint_or_command(self) -> McpServerCreate:
        if self.transport == Transport.HTTP and not self.endpoint:
            raise ValueError("endpoint is required for http transport")
        if self.transport == Transport.SSE and not self.endpoint:
            raise ValueError("endpoint is required for sse transport")
        if self.transport == Transport.STDIO and not self.command:
            raise ValueError("command is required for stdio transport")
        return self


class McpServerUpdate(BaseModel):
    transport: Transport | None = None
    endpoint: str | None = Field(None, max_length=500)
    command: str | None = Field(None, max_length=500)
    env: dict[str, str] | None = None
    enabled: bool | None = None
    headers: dict[str, str] | None = None
    auth: McpAuthConfig | None = None
    timeout_seconds: int | None = Field(None, ge=1)
    tool_filter: McpToolFilter | None = None
    args: list[str] | None = None


class McpServerOut(BaseModel):
    id: str
    tenant_id: str
    name: str
    transport: Transport
    endpoint: str | None = None
    command: str | None = None
    env: dict[str, Any] = Field(default_factory=dict)
    enabled: bool
    headers: dict[str, Any] = Field(default_factory=dict)
    auth: dict[str, Any] | None = None
    timeout_seconds: int | None = None
    tool_filter: dict[str, Any] = Field(default_factory=lambda: {"allow": [], "block": []})
    args: list[str] = Field(default_factory=list)
    # Discovered tool list persisted by the /test endpoint on a successful
    # handshake.  Empty list until the first successful test.  Reuses
    # ``McpTestTool`` so the stored shape and the probe/test response shape
    # are the same type.
    discovered_tools: list[McpTestTool] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)

    @field_validator("tool_filter", mode="before")
    @classmethod
    def _normalize_tool_filter(cls, v: Any) -> dict[str, list[str]]:
        if v is None:
            return {"allow": [], "block": []}
        if isinstance(v, dict):
            allow = v.get("allow", [])
            block = v.get("block", [])
            return {
                "allow": list(allow) if isinstance(allow, list) else [],
                "block": list(block) if isinstance(block, list) else [],
            }
        return {"allow": [], "block": []}


class McpProbeRequest(BaseModel):
    """Ad-hoc connection config for the id-less ``POST /mcp-servers/probe``.

    The admin console needs the upstream's tool list *before* a server row
    exists (the "create" form) as well as after (the "edit" form), so this
    carries the connection fields verbatim from the form.  ``auth`` arrives as
    plaintext — nothing has been encrypted yet — so the probe path must not run
    it through ``decrypt_auth``.  Field types mirror the matching
    ``McpServerCreate`` fields; ``name`` / ``command`` / ``env`` / ``args`` /
    ``tool_filter`` are omitted because the HTTP handshake never reads them.
    """

    transport: Transport
    endpoint: str | None = Field(None, max_length=500)
    headers: dict[str, str] = Field(default_factory=dict)
    auth: McpAuthConfig | None = None
    timeout_seconds: int | None = Field(None, ge=1)


class McpServerTestOverride(BaseModel):
    """Optional body for ``POST /mcp-servers/{server_id}/test``.

    Every field is optional: an empty body (``{}`` — what the BFF forwards when
    the client sends none) probes the stored config unchanged, and any field
    that *is* present replaces the stored value for this one probe.  An
    overridden ``auth`` is plaintext from the admin form, so it bypasses the
    stored-secret decrypt path.

    One asymmetry, enforced by the route rather than the schema (it needs the
    stored row to compare against): overriding ``endpoint`` to a *different*
    value than the stored one forfeits stored credentials — ``auth`` and
    ``headers`` must then be supplied explicitly, or the probe is refused.
    Otherwise a read-only caller could redirect the probe at a URL they control
    and collect the tenant's decrypted secret.
    """

    transport: Transport | None = None
    endpoint: str | None = Field(None, max_length=500)
    headers: dict[str, str] | None = None
    auth: McpAuthConfig | None = None
    timeout_seconds: int | None = Field(None, ge=1)


class McpTestResult(BaseModel):
    ok: bool
    server_info: dict[str, Any] | None = None
    tools: list[McpTestTool] = Field(default_factory=list)
    error: str | None = None
