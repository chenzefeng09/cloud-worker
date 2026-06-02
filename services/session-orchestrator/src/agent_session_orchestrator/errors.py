"""Service-specific error codes — RFC 9457 Problem Details."""


class OrchestratorError(Exception):
    """Base error for session-orchestrator."""

    def __init__(self, detail: str, *, code: str, status: int = 400) -> None:
        self.detail = detail
        self.code = code
        self.status = status


class SessionNotFoundError(OrchestratorError):
    def __init__(self, session_id: str) -> None:
        super().__init__(
            f"Session {session_id} not found",
            code="AGENT.SESSION.NOT_FOUND",
            status=404,
        )


class SessionAccessDeniedError(OrchestratorError):
    def __init__(self, session_id: str) -> None:
        super().__init__(
            f"Access denied to session {session_id}",
            code="AGENT.SESSION.ACCESS_DENIED",
            status=403,
        )


class InvalidTransitionError(OrchestratorError):
    def __init__(self, current: str, target: str) -> None:
        super().__init__(
            f"Cannot transition from {current} to {target}",
            code="AGENT.SESSION.INVALID_TRANSITION",
            status=409,
        )


class RunNotFoundError(OrchestratorError):
    def __init__(self, run_id: str) -> None:
        super().__init__(
            f"Run {run_id} not found",
            code="AGENT.RUN.NOT_FOUND",
            status=404,
        )
