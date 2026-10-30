"""Service-specific error codes — RFC 9457 Problem Details."""

from __future__ import annotations


class RegistryError(Exception):
    def __init__(self, detail: str, *, code: str, status: int = 400) -> None:
        self.detail = detail
        self.code = code
        self.status = status


class McpServerNotFoundError(RegistryError):
    def __init__(self, server_id: str) -> None:
        super().__init__(
            f"MCP server {server_id} not found",
            code="MCP.SERVER.NOT_FOUND",
            status=404,
        )


class McpServerNameConflictError(RegistryError):
    def __init__(self, name: str) -> None:
        super().__init__(
            f"MCP server name {name!r} already registered for this tenant",
            code="MCP.SERVER.NAME_CONFLICT",
            status=409,
        )


class InvalidTransportError(RegistryError):
    def __init__(self, transport: str) -> None:
        super().__init__(
            f"Unsupported transport {transport!r} (expected http|stdio)",
            code="MCP.SERVER.INVALID_TRANSPORT",
            status=422,
        )


class InternalScopeRequiredError(RegistryError):
    def __init__(self) -> None:
        super().__init__(
            "Internal endpoint requires service-account scope mcp.read_internal",
            code="MCP.AUTH.INTERNAL_SCOPE_REQUIRED",
            status=403,
        )


class SecretEncryptionUnavailableError(RegistryError):
    def __init__(self) -> None:
        super().__init__(
            "Secrets can't be saved right now because the encryption service is unavailable",
            code="MCP.CRYPTO.UNAVAILABLE",
            status=503,
        )


class MaskedValueNotStoredError(RegistryError):
    """A PATCH kept a masked ``env`` / ``headers`` value whose key has nothing stored."""

    def __init__(self, field: str, keys: list[str]) -> None:
        super().__init__(
            f"No saved value to keep for {field} {', '.join(keys)}; enter the value again",
            code="MCP.SERVER.MASKED_VALUE_NOT_STORED",
            status=422,
        )
