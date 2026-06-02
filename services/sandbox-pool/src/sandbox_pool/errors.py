"""Pool-specific error codes."""

from __future__ import annotations


class PoolError(Exception):
    def __init__(self, detail: str, *, code: str, status: int = 400) -> None:
        self.detail = detail
        self.code = code
        self.status = status


class PoolExhaustedError(PoolError):
    def __init__(self) -> None:
        super().__init__(
            "Pool max_total reached; refusing new acquire",
            code="AGENT.POOL.EXHAUSTED",
            status=429,
        )


class SandboxNotFoundError(PoolError):
    def __init__(self, sandbox_id: str) -> None:
        super().__init__(
            f"Sandbox {sandbox_id} not in pool",
            code="AGENT.POOL.NOT_FOUND",
            status=404,
        )
