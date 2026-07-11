"""Service-specific error codes — RFC 9457 Problem Details."""


class OrchestratorError(Exception):
    """Base error for session-orchestrator.

    ``user_message`` is an optional Chinese, user-facing string.  When present
    the exception handler surfaces it alongside the RFC 9457 body so the
    frontend can render it directly without stringifying the whole problem
    (see BFF ``canvas.py`` for the same convention).  It defaults to ``detail``
    when omitted.
    """

    def __init__(
        self,
        detail: str,
        *,
        code: str,
        status: int = 400,
        user_message: str | None = None,
    ) -> None:
        self.detail = detail
        self.code = code
        self.status = status
        self.user_message = user_message or detail


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


# ── Canvas v2 errors (AGENT.SESSION.CANVAS.*) ───────────────────────────────


class CanvasNotFoundError(OrchestratorError):
    def __init__(self, canvas_id: str) -> None:
        super().__init__(
            f"Canvas {canvas_id} not found",
            code="AGENT.SESSION.CANVAS.NOT_FOUND",
            status=404,
            user_message="画布不存在或已被删除。",
        )


class CanvasVersionNotFoundError(OrchestratorError):
    def __init__(self, canvas_id: str, version: int) -> None:
        super().__init__(
            f"Canvas {canvas_id} has no version {version}",
            code="AGENT.SESSION.CANVAS.VERSION_NOT_FOUND",
            status=404,
            user_message="指定的画布版本不存在。",
        )


class CanvasVersionConflictError(OrchestratorError):
    def __init__(self, base_version: int, current_version: int) -> None:
        super().__init__(
            f"base_version {base_version} != current_version {current_version}",
            code="AGENT.SESSION.CANVAS.VERSION_CONFLICT",
            status=409,
            user_message="画布已被更新，请刷新后重试。",
        )


class CanvasSuggestionResolvedError(OrchestratorError):
    def __init__(self, suggestion_id: str, status_value: str) -> None:
        super().__init__(
            f"Suggestion {suggestion_id} already {status_value}",
            code="AGENT.SESSION.CANVAS.SUGGESTION_RESOLVED",
            status=409,
            user_message="该修改建议已被处理，请勿重复操作。",
        )


class CanvasBadRequestError(OrchestratorError):
    def __init__(self, detail: str, *, user_message: str) -> None:
        super().__init__(
            detail,
            code="AGENT.SESSION.CANVAS.BAD_REQUEST",
            status=422,
            user_message=user_message,
        )
