"""Translate FetchGatewayError into RFC 9457 Problem Details responses."""

from __future__ import annotations

import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from web_fetch_gateway.errors import FetchGatewayError


def _get_request_id(request: Request) -> str:
    """Return the request-id attached by middleware, or generate a fallback."""
    # Check if foundation_sdk request context is available; if not, fall back
    # to a header or a freshly generated UUID.
    rid = request.headers.get("x-request-id")
    if rid:
        return rid
    # Try the state attribute set by install_request_context (foundation_sdk).
    try:
        return str(request.state.request_id)
    except AttributeError:
        return str(uuid.uuid4())


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(FetchGatewayError)
    async def _fetch_error_handler(
        request: Request, exc: FetchGatewayError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status,
            content={
                "type": (
                    f"https://agent-platform.dev/errors/"
                    f"{exc.code.split('.')[-1].lower()}"
                ),
                "title": exc.code,
                "status": exc.status,
                "detail": exc.detail,
                "instance": str(request.url.path),
                "code": exc.code,
                "request_id": _get_request_id(request),
            },
        )
