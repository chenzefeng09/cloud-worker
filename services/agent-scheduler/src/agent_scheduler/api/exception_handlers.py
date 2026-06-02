"""Scheduler-specific exception handlers.

The generic RFC 9457 fallback for unhandled ``Exception`` is provided by
:func:`foundation_sdk.fastapi.register_problem_details` — wired up in
``agent_scheduler.main.create_app``.  This module only registers handlers
for the scheduler's own typed errors.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from agent_scheduler.api.middleware import get_request_id
from agent_scheduler.errors import SchedulerError


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(SchedulerError)
    async def scheduler_error_handler(
        request: Request, exc: SchedulerError,
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
                "request_id": get_request_id(),
            },
        )
