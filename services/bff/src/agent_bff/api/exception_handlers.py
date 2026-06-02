"""BFF-specific exception handlers.

The generic RFC 9457 fallback for unhandled ``Exception`` is provided by
:func:`foundation_sdk.fastapi.register_problem_details` — see
``services/bff/src/agent_bff/main.py`` where it's wired in.  This module
only registers handlers for the BFF's own typed errors.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from agent_bff.api.middleware import get_request_id
from agent_bff.errors import BFFError


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(BFFError)
    async def bff_error_handler(request: Request, exc: BFFError) -> JSONResponse:
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
