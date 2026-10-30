"""Service-specific errors."""


class SchedulerError(Exception):
    def __init__(self, detail: str, *, code: str, status: int = 400) -> None:
        self.detail = detail
        self.code = code
        self.status = status


class ContainerNotFoundError(SchedulerError):
    def __init__(self, container_id: str) -> None:
        super().__init__(
            f"Container {container_id} not found",
            code="AGENT.SCHEDULER.CONTAINER_NOT_FOUND",
            status=404,
        )


class SandboxSpawnError(SchedulerError):
    def __init__(self, detail: str) -> None:
        super().__init__(
            detail,
            code="AGENT.SCHEDULER.SPAWN_FAILED",
            status=500,
        )


class LlmNotConfiguredError(SchedulerError):
    """The tenant has no enabled LLM provider, so no run can call a model.

    A tenant setting, not an outage: retrying cannot help until an
    administrator adds a provider.
    """

    def __init__(self) -> None:
        super().__init__(
            "No enabled LLM provider is configured for this tenant",
            code="AGENT.SCHEDULER.LLM_NOT_CONFIGURED",
            status=422,
        )


class LlmGatewayUnavailableError(SchedulerError):
    """No model key could be obtained because the LLM gateway did not answer."""

    def __init__(self) -> None:
        super().__init__(
            "LLM gateway virtual key unavailable for tenant",
            code="AGENT.SCHEDULER.LLM_GATEWAY_UNAVAILABLE",
            status=502,
        )
