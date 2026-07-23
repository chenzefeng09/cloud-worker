"""Translate CronRunnerError into RFC 9457 Problem Details.

cron-runner does not depend on foundation_sdk, so this handler is
self-contained; it mirrors the problem+json shape used by the sibling
services (skill-store / app-registry).
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from cron_runner.errors import CronRunnerError


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(CronRunnerError)
    async def _cron_runner_error(
        request: Request, exc: CronRunnerError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status,
            content={
                "type": f"https://agent-platform.dev/errors/{exc.code.split('.')[-1].lower()}",
                "title": exc.code,
                "status": exc.status,
                "detail": exc.detail,
                "instance": str(request.url.path),
                "code": exc.code,
                "user_message": exc.user_message,
            },
        )
