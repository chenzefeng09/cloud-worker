"""Small stateless MCP server used by the local development stack.

The seeded resource-library entry points to ``http://echo-mcp:9000/mcp``.
Keeping the service in Compose makes that example an actual health-checkable
MCP integration instead of a permanently enabled but unreachable record.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request, Response, status
from fastapi.responses import JSONResponse

app = FastAPI(title="Development Echo MCP", docs_url=None, redoc_url=None)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


def _result(request_id: Any, result: dict[str, Any]) -> JSONResponse:
    return JSONResponse(
        {"jsonrpc": "2.0", "id": request_id, "result": result},
        headers={"MCP-Protocol-Version": "2024-11-05"},
    )


@app.post("/mcp")
async def mcp(request: Request) -> Response:
    payload = await request.json()
    method = str(payload.get("method") or "")
    request_id = payload.get("id")

    if method == "notifications/initialized":
        return Response(status_code=status.HTTP_202_ACCEPTED)
    if method == "initialize":
        return _result(
            request_id,
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "echo", "version": "1.0.0"},
            },
        )
    if method == "tools/list":
        return _result(
            request_id,
            {
                "tools": [
                    {
                        "name": "echo",
                        "description": "原样返回输入文本，用于验证 MCP 连接和工具调用。",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "message": {
                                    "type": "string",
                                    "description": "需要回显的文本",
                                }
                            },
                            "required": ["message"],
                            "additionalProperties": False,
                        },
                    }
                ]
            },
        )
    if method == "tools/call":
        params = payload.get("params") or {}
        if params.get("name") != "echo":
            return _result(
                request_id,
                {
                    "content": [{"type": "text", "text": "未知工具"}],
                    "isError": True,
                },
            )
        arguments = params.get("arguments") or {}
        message = str(arguments.get("message") or "")
        return _result(
            request_id,
            {
                "content": [{"type": "text", "text": message}],
                "structuredContent": {"message": message},
                "isError": False,
            },
        )

    return JSONResponse(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        },
        status_code=status.HTTP_404_NOT_FOUND,
    )
