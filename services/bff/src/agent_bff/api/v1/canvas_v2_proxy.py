"""Canvas v2 CRUD/版本/建议端点 —— 纯代理转发到 session-orchestrator。

orchestrator 是画布对象的 system of record（实体/版本/建议落库），前端不能
直连 orchestrator，BFF 只做认证 + 转发（method/path/headers/body 原样转发，
状态码与 JSON body 原样中继）。与 ``canvas_v2.py`` 的 LLM 边缘编辑端点
（``POST /sessions/{sid}/canvases/{cid}/edit``）分开放置 —— 那个端点要拼
prompt、调 gateway、做 schema 校验，这里全是哑代理。

不代理 ``POST /canvases/{cid}/suggestions``：那是 orchestrator-internal 端点，
只由 ``canvas_v2.py`` 的编辑端点调用，前端从不直接调用它。
"""

from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, Depends, Request
from starlette.responses import JSONResponse

from agent_bff.api._http import foundation_headers
from agent_bff.api.dependencies import require_auth

router = APIRouter(tags=["Canvas"])
logger = logging.getLogger(__name__)

_TIMEOUT = 30


async def _proxy(
    *,
    method: str,
    orch_url: str,
    orch_path: str,
    tenant_id: str,
    user_id: str,
    access_token: str,
    json_body: dict | None = None,
) -> JSONResponse:
    """转发一次请求到 orchestrator，状态码 + JSON body 原样中继。

    ``orch_path`` 是相对 ``orch_url``（即 ``session_orchestrator_url``，已含
    ``/api/session/v1``）的路径，以 ``/`` 开头。
    """
    headers = foundation_headers(tenant_id, user_id, access_token)
    url = f"{orch_url.rstrip('/')}{orch_path}"

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        try:
            if method == "GET":
                resp = await client.get(url, headers=headers)
            elif method == "DELETE":
                resp = await client.delete(url, headers=headers)
            else:
                resp = await client.post(url, json=json_body, headers=headers)
        except httpx.HTTPError as exc:
            logger.warning(
                "canvas v2 proxy: orchestrator unreachable (%s %s): %s", method, url, exc
            )
            return JSONResponse(
                status_code=502,
                content={"detail": "画布服务暂时不可用，请稍后重试。", "error": str(exc)},
            )

    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        # 204 (or any empty body) — faithfully relay the status with empty JSON.
        if not resp.content:
            return JSONResponse(
                status_code=resp.status_code if resp.status_code < 600 else 502,
                content=None,
            )
        body = {"detail": "Upstream returned non-JSON response", "status": resp.status_code}

    return JSONResponse(
        status_code=resp.status_code if resp.status_code < 600 else 502,
        content=body,
    )


# ─────────────────────────────────────────────────────────────────────────────
# 画布 CRUD
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/sessions/{session_id}/canvases")
async def create_canvas(
    session_id: str,
    request: Request,
    _auth: tuple = Depends(require_auth()),
) -> JSONResponse:
    """创建画布 —— 代理到 orchestrator。"""
    tenant_id, user_id, access_token = _auth
    orch_url = request.app.state.settings.session_orchestrator_url
    body = await request.json()
    return await _proxy(
        method="POST",
        orch_url=orch_url,
        orch_path=f"/sessions/{session_id}/canvases",
        tenant_id=tenant_id,
        user_id=user_id,
        access_token=access_token,
        json_body=body,
    )


@router.get("/sessions/{session_id}/canvases")
async def list_canvases(
    session_id: str,
    request: Request,
    _auth: tuple = Depends(require_auth()),
) -> JSONResponse:
    """列出会话下的画布 —— 代理到 orchestrator。"""
    tenant_id, user_id, access_token = _auth
    orch_url = request.app.state.settings.session_orchestrator_url
    return await _proxy(
        method="GET",
        orch_url=orch_url,
        orch_path=f"/sessions/{session_id}/canvases",
        tenant_id=tenant_id,
        user_id=user_id,
        access_token=access_token,
    )


@router.get("/canvases/{canvas_id}")
async def get_canvas(
    canvas_id: str,
    request: Request,
    _auth: tuple = Depends(require_auth()),
) -> JSONResponse:
    """读取画布详情（含当前版本 blocks）—— 代理到 orchestrator。"""
    tenant_id, user_id, access_token = _auth
    orch_url = request.app.state.settings.session_orchestrator_url
    return await _proxy(
        method="GET",
        orch_url=orch_url,
        orch_path=f"/canvases/{canvas_id}",
        tenant_id=tenant_id,
        user_id=user_id,
        access_token=access_token,
    )


@router.delete("/canvases/{canvas_id}", status_code=204)
async def delete_canvas(
    canvas_id: str,
    request: Request,
    _auth: tuple = Depends(require_auth()),
) -> JSONResponse:
    """软删除画布（Artifacts 专区删除）—— 代理到 orchestrator。

    转发 DELETE，状态码原样中继（成功为 204）。orchestrator 的
    ``CanvasService.soft_delete`` 只翻 ``deleted_at``，行与版本留待保留期
    清理统一物理删除。
    """
    tenant_id, user_id, access_token = _auth
    orch_url = request.app.state.settings.session_orchestrator_url
    return await _proxy(
        method="DELETE",
        orch_url=orch_url,
        orch_path=f"/canvases/{canvas_id}",
        tenant_id=tenant_id,
        user_id=user_id,
        access_token=access_token,
    )


# ─────────────────────────────────────────────────────────────────────────────
# 版本
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/canvases/{canvas_id}/versions")
async def list_versions(
    canvas_id: str,
    request: Request,
    _auth: tuple = Depends(require_auth()),
) -> JSONResponse:
    """列出画布的历史版本 —— 代理到 orchestrator。"""
    tenant_id, user_id, access_token = _auth
    orch_url = request.app.state.settings.session_orchestrator_url
    return await _proxy(
        method="GET",
        orch_url=orch_url,
        orch_path=f"/canvases/{canvas_id}/versions",
        tenant_id=tenant_id,
        user_id=user_id,
        access_token=access_token,
    )


@router.get("/canvases/{canvas_id}/versions/{version}")
async def get_version(
    canvas_id: str,
    version: str,
    request: Request,
    _auth: tuple = Depends(require_auth()),
) -> JSONResponse:
    """读取画布的某个历史版本 —— 代理到 orchestrator。"""
    tenant_id, user_id, access_token = _auth
    orch_url = request.app.state.settings.session_orchestrator_url
    return await _proxy(
        method="GET",
        orch_url=orch_url,
        orch_path=f"/canvases/{canvas_id}/versions/{version}",
        tenant_id=tenant_id,
        user_id=user_id,
        access_token=access_token,
    )


@router.post("/canvases/{canvas_id}/versions")
async def create_version(
    canvas_id: str,
    request: Request,
    _auth: tuple = Depends(require_auth()),
) -> JSONResponse:
    """手动编辑落一个新版本 —— 代理到 orchestrator（可能 409 base_version 冲突，原样中继）。"""
    tenant_id, user_id, access_token = _auth
    orch_url = request.app.state.settings.session_orchestrator_url
    body = await request.json()
    return await _proxy(
        method="POST",
        orch_url=orch_url,
        orch_path=f"/canvases/{canvas_id}/versions",
        tenant_id=tenant_id,
        user_id=user_id,
        access_token=access_token,
        json_body=body,
    )


# ─────────────────────────────────────────────────────────────────────────────
# 建议：接受 / 拒绝
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/canvases/{canvas_id}/suggestions/{suggestion_id}/accept")
async def accept_suggestion(
    canvas_id: str,
    suggestion_id: str,
    request: Request,
    _auth: tuple = Depends(require_auth()),
) -> JSONResponse:
    """接受一条修改建议 —— 代理到 orchestrator。"""
    tenant_id, user_id, access_token = _auth
    orch_url = request.app.state.settings.session_orchestrator_url
    body = await request.json() if await _has_body(request) else None
    return await _proxy(
        method="POST",
        orch_url=orch_url,
        orch_path=f"/canvases/{canvas_id}/suggestions/{suggestion_id}/accept",
        tenant_id=tenant_id,
        user_id=user_id,
        access_token=access_token,
        json_body=body,
    )


@router.post("/canvases/{canvas_id}/suggestions/{suggestion_id}/reject")
async def reject_suggestion(
    canvas_id: str,
    suggestion_id: str,
    request: Request,
    _auth: tuple = Depends(require_auth()),
) -> JSONResponse:
    """拒绝一条修改建议 —— 代理到 orchestrator。"""
    tenant_id, user_id, access_token = _auth
    orch_url = request.app.state.settings.session_orchestrator_url
    body = await request.json() if await _has_body(request) else None
    return await _proxy(
        method="POST",
        orch_url=orch_url,
        orch_path=f"/canvases/{canvas_id}/suggestions/{suggestion_id}/reject",
        tenant_id=tenant_id,
        user_id=user_id,
        access_token=access_token,
        json_body=body,
    )


# ─────────────────────────────────────────────────────────────────────────────
# 回退
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/canvases/{canvas_id}/revert")
async def revert_canvas(
    canvas_id: str,
    request: Request,
    _auth: tuple = Depends(require_auth()),
) -> JSONResponse:
    """回退画布到某个历史版本 —— 代理到 orchestrator。"""
    tenant_id, user_id, access_token = _auth
    orch_url = request.app.state.settings.session_orchestrator_url
    body = await request.json()
    return await _proxy(
        method="POST",
        orch_url=orch_url,
        orch_path=f"/canvases/{canvas_id}/revert",
        tenant_id=tenant_id,
        user_id=user_id,
        access_token=access_token,
        json_body=body,
    )


async def _has_body(request: Request) -> bool:
    """accept/reject 前端可能不带 body；无 body 时不强求解析 JSON。"""
    raw = await request.body()
    return bool(raw)
