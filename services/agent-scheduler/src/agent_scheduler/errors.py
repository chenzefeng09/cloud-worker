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
