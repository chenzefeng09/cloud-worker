"""Service-specific error codes."""

from __future__ import annotations


class CronRunnerError(Exception):
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


class CronJobNotFoundError(CronRunnerError):
    def __init__(self, job_id: str) -> None:
        super().__init__(
            f"CronJob {job_id} not found",
            code="CRON.NOT_FOUND",
            status=404,
        )


class InvalidCronExpressionError(CronRunnerError):
    def __init__(self, expr: str, reason: str) -> None:
        super().__init__(
            f"Invalid cron expression {expr!r}: {reason}",
            code="CRON.INVALID_EXPRESSION",
            status=400,
        )
