"""BFF errors."""


class BFFError(Exception):
    def __init__(self, detail: str, *, code: str, status: int = 400) -> None:
        self.detail = detail
        self.code = code
        self.status = status


class AuthenticationError(BFFError):
    def __init__(self, detail: str = "Authentication failed") -> None:
        super().__init__(detail, code="AGENT.BFF.AUTH_FAILED", status=401)


class ProxyError(BFFError):
    def __init__(self, upstream: str, detail: str) -> None:
        super().__init__(
            f"Upstream {upstream} error: {detail}",
            code="AGENT.BFF.UPSTREAM_ERROR",
            status=502,
        )
