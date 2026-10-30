"""BFF-specific exception handlers.

The generic RFC 9457 fallback for unhandled ``Exception`` is provided by
:func:`foundation_sdk.fastapi.register_problem_details` — see
``services/bff/src/agent_bff/main.py`` where it's wired in.  This module
registers handlers for the BFF's own typed errors, and for upstream failures a
route did not translate itself: those are the upstream's answer (or its
absence), never a BFF internal error.
"""

from __future__ import annotations

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from agent_bff.api.middleware import get_request_id
from agent_bff.errors import BFFError, ProxyError


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

    @app.exception_handler(httpx.HTTPStatusError)
    async def upstream_status_handler(
        request: Request, exc: httpx.HTTPStatusError,
    ) -> JSONResponse:
        """Relay an upstream error status a route left to ``raise_for_status()``.

        Same shape as the routes' ``_raise_for_upstream``: the upstream status
        with its body as ``detail`` — so a 403 (no permission) or a 404 reaches
        the SPA as such instead of as a 500.
        """
        resp = exc.response
        try:
            detail = resp.json()
        except ValueError:
            detail = resp.text
        status = resp.status_code if resp.status_code >= 400 else 502
        return JSONResponse(status_code=status, content={"detail": detail})

    @app.exception_handler(httpx.TransportError)
    async def upstream_unreachable_handler(
        request: Request, exc: httpx.TransportError,
    ) -> JSONResponse:
        """A service that could not be reached (or timed out) is a 502."""
        return await bff_error_handler(request, ProxyError("service", "unreachable"))
