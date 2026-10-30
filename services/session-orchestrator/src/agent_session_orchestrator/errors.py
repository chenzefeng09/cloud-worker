"""Service-specific error codes — RFC 9457 Problem Details."""


class OrchestratorError(Exception):
    """Base error for session-orchestrator.

    ``user_message`` is an optional English, user-facing string.  When present
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


# The scheduler refusals a user can act on.  Neither goes away by retrying,
# so the generic "try again" a refused run otherwise gets would mislead.


class LlmNotConfiguredError(OrchestratorError):
    def __init__(self) -> None:
        super().__init__(
            "No enabled LLM provider is configured for this tenant",
            code="AGENT.RUN.LLM_NOT_CONFIGURED",
            status=422,
            user_message=(
                "No AI model is set up for your organization yet, so the agent "
                "can't run. An administrator can add one under Admin → Models."
            ),
        )


class RunQuotaExceededError(OrchestratorError):
    def __init__(self) -> None:
        super().__init__(
            "The tenant's agent session quota is exhausted",
            code="AGENT.RUN.QUOTA_EXCEEDED",
            status=402,
            user_message=(
                "Your organization has reached its limit for agent runs. "
                "An administrator can review it under Admin → Subscriptions & quotas."
            ),
        )


# ── Workspace errors (AGENT.WORKSPACE.*) ────────────────────────────────────


class WorkspaceNotFoundError(OrchestratorError):
    def __init__(self, workspace_id: str) -> None:
        super().__init__(
            f"Workspace {workspace_id} not found",
            code="AGENT.WORKSPACE.NOT_FOUND",
            status=404,
            user_message="This workspace does not exist or has been deleted.",
        )


class WorkspaceFileNotFoundError(OrchestratorError):
    def __init__(self, file_id: str) -> None:
        super().__init__(
            f"Workspace file {file_id} not found",
            code="AGENT.WORKSPACE.FILE_NOT_FOUND",
            status=404,
            user_message="This workspace file does not exist or has been deleted.",
        )


class WorkspaceDefaultDeleteError(OrchestratorError):
    def __init__(self, workspace_id: str) -> None:
        super().__init__(
            f"Workspace {workspace_id} is the default workspace and cannot be deleted",
            code="AGENT.WORKSPACE.DEFAULT_DELETE",
            status=409,
            user_message="The default workspace cannot be deleted.",
        )


# ── Canvas v2 errors (AGENT.SESSION.CANVAS.*) ───────────────────────────────


class CanvasNotFoundError(OrchestratorError):
    def __init__(self, canvas_id: str) -> None:
        super().__init__(
            f"Canvas {canvas_id} not found",
            code="AGENT.SESSION.CANVAS.NOT_FOUND",
            status=404,
            user_message="This canvas does not exist or has been deleted.",
        )


class CanvasVersionNotFoundError(OrchestratorError):
    def __init__(self, canvas_id: str, version: int) -> None:
        super().__init__(
            f"Canvas {canvas_id} has no version {version}",
            code="AGENT.SESSION.CANVAS.VERSION_NOT_FOUND",
            status=404,
            user_message="The requested canvas version does not exist.",
        )


class CanvasVersionConflictError(OrchestratorError):
    def __init__(self, base_version: int, current_version: int) -> None:
        super().__init__(
            f"base_version {base_version} != current_version {current_version}",
            code="AGENT.SESSION.CANVAS.VERSION_CONFLICT",
            status=409,
            user_message="The canvas has been updated. Please refresh and try again.",
        )


class CanvasSuggestionResolvedError(OrchestratorError):
    def __init__(self, suggestion_id: str, status_value: str) -> None:
        super().__init__(
            f"Suggestion {suggestion_id} already {status_value}",
            code="AGENT.SESSION.CANVAS.SUGGESTION_RESOLVED",
            status=409,
            user_message="This suggestion has already been handled.",
        )


class CanvasBadRequestError(OrchestratorError):
    def __init__(self, detail: str, *, user_message: str) -> None:
        super().__init__(
            detail,
            code="AGENT.SESSION.CANVAS.BAD_REQUEST",
            status=422,
            user_message=user_message,
        )
