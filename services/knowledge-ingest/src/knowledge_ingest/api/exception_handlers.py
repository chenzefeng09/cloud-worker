"""Translate IngestError into RFC 9457 Problem Details."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from foundation_sdk.fastapi import get_request_id

from knowledge_ingest.errors import IngestError


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(IngestError)
    async def _err(request: Request, exc: IngestError) -> JSONResponse:
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
