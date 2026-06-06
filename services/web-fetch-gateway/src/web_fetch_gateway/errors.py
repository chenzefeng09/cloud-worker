"""Service-specific error codes — RFC 9457 Problem Details."""

from __future__ import annotations


class FetchGatewayError(Exception):
    def __init__(self, detail: str, *, code: str, status: int = 400) -> None:
        self.detail = detail
        self.code = code
        self.status = status


class DomainNotAllowedError(FetchGatewayError):
    def __init__(self, hostname: str) -> None:
        super().__init__(
            f"Domain {hostname!r} is not in the allowed-domains list",
            code="FETCH.DOMAIN_NOT_ALLOWED",
            status=403,
        )


class PrivateAddressError(FetchGatewayError):
    def __init__(self, hostname: str) -> None:
        super().__init__(
            f"Domain {hostname!r} resolves to a private/reserved IP address",
            code="FETCH.PRIVATE_ADDRESS",
            status=403,
        )


class FetchTimeoutError(FetchGatewayError):
    def __init__(self, url: str) -> None:
        super().__init__(
            f"Request to {url!r} timed out",
            code="FETCH.TIMEOUT",
            status=504,
        )


class FetchUpstreamError(FetchGatewayError):
    def __init__(self, detail: str) -> None:
        super().__init__(
            detail,
            code="FETCH.UPSTREAM_ERROR",
            status=502,
        )


class ContentTypeRejectedError(FetchGatewayError):
    def __init__(self, content_type: str) -> None:
        super().__init__(
            f"Response content-type {content_type!r} is not allowed",
            code="FETCH.CONTENT_TYPE_REJECTED",
            status=415,
        )


class FetchAuthError(FetchGatewayError):
    def __init__(self, detail: str, *, status: int = 401) -> None:
        super().__init__(
            detail,
            code="FETCH.AUTH_REQUIRED" if status == 401 else "FETCH.SCOPE_REQUIRED",
            status=status,
        )
