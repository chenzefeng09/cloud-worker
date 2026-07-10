"""Canvas v2 作用域编辑端点 —— BFF 作为 LLM 边缘，产出「建议」经 orchestrator 落库。

设计见 ``docs/canvas-v2-design.md`` §5.2。与旧 ``canvas.py`` 的 ``/canvas/edit``
（PR-6 才退役）不同，本端点走「一等对象 + 建议-确认制」协议：

  * **画布是服务端一等对象**：orchestrator 是 system of record（实体/版本/建议落库）。
    BFF 不再从散文里二次提取，也不做重叠修剪 —— 模型只往显式块槽位里生成。
  * **显式块寻址**：请求带 ``scope``（blocks + block_ids / document），BFF 从
    orchestrator 拉当前版本 blocks，组 prompt 时给目标块标 ``[block_id]``、相邻块
    只读上下文，指令要求「只返回 JSON、只改目标块、禁止复述未修改内容」。
  * **结构化输出**：gateway ``response_format=json_object``，服务端按 schema 校验
    ``{edits, insertions, change_summary}``；不合法**重试一次**（附校验错误），
    仍失败 → 502 ``AGENT.BFF.CANVAS.BAD_MODEL_OUTPUT``。
  * **建议落库**：把 edits/insertions 换算成 diffs（``[{block_id|after_block_id,
    op, orig, sugg}]``，orig 从当前 blocks 回填），POST 到 orchestrator
    ``/canvases/{cid}/suggestions``，200 返回 ``{suggestion: {...}}``。

LLM 边缘的基建（虚拟密钥缓存、默认模型解析、problem+json、超时常量）复用 ``canvas.py``
的实现（import 复用，不复制粘贴），不改变其现有行为。
"""

from __future__ import annotations

import json
import logging

import httpx
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, field_validator
from starlette.responses import JSONResponse

from agent_bff.api._http import foundation_headers
from agent_bff.api.dependencies import require_auth

# ── 复用 canvas.py 的 LLM 边缘基建（不复制粘贴） ────────────────────────────────
from agent_bff.api.v1.canvas import (
    _COMPLETION_TIMEOUT,
    _META_TIMEOUT,
    _problem,
    _resolve_default_model,
    _resolve_virtual_key,
)

router = APIRouter(tags=["Canvas"])
logger = logging.getLogger(__name__)

# 组 prompt 时目标块两侧各带多少个只读上下文块。
_CONTEXT_NEIGHBORS = 1


# ─────────────────────────────────────────────────────────────────────────────
# 请求模型
# ─────────────────────────────────────────────────────────────────────────────

class _Scope(BaseModel):
    type: str
    block_ids: list[str] | None = None

    @field_validator("type")
    @classmethod
    def _validate_type(cls, v: str) -> str:
        if v not in ("blocks", "document"):
            raise ValueError("scope.type must be 'blocks' or 'document'")
        return v


class CanvasV2EditRequest(BaseModel):
    """``POST /sessions/{sid}/canvases/{cid}/edit`` 请求体（设计 §5.2）。"""

    instruction: str
    scope: _Scope
    model: str | None = None
    base_version: int | None = None


# ─────────────────────────────────────────────────────────────────────────────
# prompt 拼接（显式块寻址 + 只读上下文）
# ─────────────────────────────────────────────────────────────────────────────

def build_scoped_edit_prompt(
    *,
    instruction: str,
    blocks: list[dict],
    target_ids: list[str],
    retry_error: str | None = None,
) -> str:
    """组作用域编辑 prompt。

    * 目标块标 ``[block_id]``，明确「可改」。
    * scope=blocks 时相邻块（各 ``_CONTEXT_NEIGHBORS`` 个）作**只读上下文**带上，
      指令要求禁止改动/复述上下文块。
    * 要求模型只返回 JSON（``response_format=json_object`` 已在 payload 里声明）。
    * ``retry_error`` 非空时（重试路径）把上次的校验错误附在最前，要求修正。

    返回单条 user message 文本。
    """
    id_to_index = {b["id"]: i for i, b in enumerate(blocks)}
    target_set = set(target_ids)

    # 需要呈现给模型的块：目标块 + 目标块相邻的上下文块。全量渲染时（document）
    # 目标即全部，无额外上下文块。
    show_indices: set[int] = set()
    for bid in target_ids:
        idx = id_to_index.get(bid)
        if idx is None:
            continue
        show_indices.add(idx)
        for off in range(1, _CONTEXT_NEIGHBORS + 1):
            if idx - off >= 0:
                show_indices.add(idx - off)
            if idx + off < len(blocks):
                show_indices.add(idx + off)

    lines: list[str] = []
    if retry_error:
        lines.extend(
            [
                "上一次的输出未通过校验，错误如下，请修正后重新只返回 JSON：",
                retry_error,
                "",
            ]
        )

    lines.extend(
        [
            "你是专业编辑。下面是一篇文档的若干块（block），每块带一个稳定的 block_id。",
            "请按「修改要求」修改**标记为【可改】的目标块**。",
            "",
            "严格约束：",
            "- 只返回一个 JSON 对象，不要有任何 JSON 以外的文字、解释或代码围栏。",
            "- edits 里的 block_id 只能是【可改】的目标块 id，禁止改动【只读上下文】块。",
            "- 只输出需要改动的块；未改动的块不要出现在 edits 里，禁止复述其原文。",
            "- op=replace 时 new_text 为该块的完整新内容；"
            "op=delete 时删除该块（不需要 new_text）。",
            "- 需要新增块时放进 insertions，after_block_id 指明插在哪个块之后。",
            "",
            "输出 JSON schema：",
            json.dumps(
                {
                    "edits": [
                        {"block_id": "blk_xxx", "op": "replace|delete", "new_text": "…"}
                    ],
                    "insertions": [{"after_block_id": "blk_xxx", "new_text": "…"}],
                    "change_summary": "一句话变更说明",
                },
                ensure_ascii=False,
            ),
            "",
            f"修改要求：{instruction}",
            "",
            "文档块：",
        ]
    )

    for idx in sorted(show_indices):
        b = blocks[idx]
        role = "【可改】" if b["id"] in target_set else "【只读上下文】"
        lines.append(f"[{b['id']}] {role}")
        lines.append(str(b.get("text", "")))
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


# ─────────────────────────────────────────────────────────────────────────────
# 模型输出解析 + schema 校验
# ─────────────────────────────────────────────────────────────────────────────

class ModelOutputError(ValueError):
    """模型输出解析/校验失败，message 携带可回灌重试 prompt 的错误描述。"""


def _strip_json_fence(text: str) -> str:
    """剥掉最外层可能的 ```json … ``` 围栏，容忍模型不守 json_object 约束。"""
    s = text.strip()
    if s.startswith("```"):
        # 去掉首行围栏（```json / ```）与末行围栏。
        first_nl = s.find("\n")
        if first_nl != -1:
            s = s[first_nl + 1 :]
        if s.rstrip().endswith("```"):
            s = s.rstrip()[: -3]
    return s.strip()


def parse_and_validate_output(
    content: str, *, target_ids: list[str]
) -> tuple[list[dict], list[dict], str | None]:
    """解析并 schema 校验模型输出，返回 ``(edits, insertions, change_summary)``。

    校验规则（设计 §5.2）：
      * 顶层必须是 JSON object。
      * ``edits`` 每项：block_id ∈ target_ids（作用域内），op ∈ replace/delete；
        op=replace 必须有非空 new_text。
      * ``insertions`` 每项：after_block_id 必须在作用域内（目标块之后插入），
        new_text 非空。
      * ``change_summary`` 可空。

    任一不满足抛 ``ModelOutputError``（message 可直接回灌重试 prompt）。
    """
    raw = _strip_json_fence(content)
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        raise ModelOutputError(f"输出不是合法 JSON：{exc}") from exc

    if not isinstance(data, dict):
        raise ModelOutputError("输出顶层必须是 JSON 对象。")

    target_set = set(target_ids)

    edits_raw = data.get("edits", [])
    insertions_raw = data.get("insertions", [])
    change_summary = data.get("change_summary")

    if not isinstance(edits_raw, list):
        raise ModelOutputError("edits 必须是数组。")
    if not isinstance(insertions_raw, list):
        raise ModelOutputError("insertions 必须是数组。")

    edits: list[dict] = []
    for i, item in enumerate(edits_raw):
        if not isinstance(item, dict):
            raise ModelOutputError(f"edits[{i}] 必须是对象。")
        bid = item.get("block_id")
        op = item.get("op")
        if not isinstance(bid, str) or bid not in target_set:
            raise ModelOutputError(
                f"edits[{i}].block_id={bid!r} 不在可编辑目标块范围内，"
                f"只能是 {sorted(target_set)} 之一。"
            )
        if op not in ("replace", "delete"):
            raise ModelOutputError(f"edits[{i}].op={op!r} 必须是 'replace' 或 'delete'。")
        new_text = item.get("new_text")
        if op == "replace":
            if not isinstance(new_text, str) or not new_text.strip():
                raise ModelOutputError(f"edits[{i}] op=replace 必须带非空 new_text。")
            edits.append({"block_id": bid, "op": "replace", "new_text": new_text})
        else:  # delete
            edits.append({"block_id": bid, "op": "delete"})

    insertions: list[dict] = []
    for i, item in enumerate(insertions_raw):
        if not isinstance(item, dict):
            raise ModelOutputError(f"insertions[{i}] 必须是对象。")
        after = item.get("after_block_id")
        new_text = item.get("new_text")
        if not isinstance(after, str) or after not in target_set:
            raise ModelOutputError(
                f"insertions[{i}].after_block_id={after!r} 不在可编辑目标块范围内，"
                f"只能是 {sorted(target_set)} 之一。"
            )
        if not isinstance(new_text, str) or not new_text.strip():
            raise ModelOutputError(f"insertions[{i}] 必须带非空 new_text。")
        insertions.append({"after_block_id": after, "new_text": new_text})

    if change_summary is not None and not isinstance(change_summary, str):
        raise ModelOutputError("change_summary 必须是字符串或省略。")

    if not edits and not insertions:
        raise ModelOutputError("edits 与 insertions 均为空，未产生任何修改。")

    summary = change_summary.strip() if isinstance(change_summary, str) else None
    return edits, insertions, (summary or None)


# ─────────────────────────────────────────────────────────────────────────────
# edits/insertions → diffs 换算（orig 从当前 blocks 回填）
# ─────────────────────────────────────────────────────────────────────────────

def build_diffs(
    *,
    edits: list[dict],
    insertions: list[dict],
    blocks: list[dict],
) -> list[dict]:
    """把结构化输出换算成 orchestrator suggestion 落库用的 diffs。

    diffs 项形状（设计 §3.3 / §5.2）：
      * replace: ``{block_id, op: "replace", orig: <当前块文本>, sugg: <新文本>}``
      * delete:  ``{block_id, op: "delete", orig: <当前块文本>, sugg: None}``
      * insert:  ``{block_id: <锚点块>, op: "insert_after", orig: None, sugg: <新文本>}``

    ``orig`` 从当前版本 blocks 按 id 回填，供前端 orig/sugg 对照直接用。
    """
    text_by_id = {b["id"]: b.get("text", "") for b in blocks}
    diffs: list[dict] = []

    for e in edits:
        bid = e["block_id"]
        if e["op"] == "replace":
            diffs.append(
                {
                    "block_id": bid,
                    "op": "replace",
                    "orig": text_by_id.get(bid, ""),
                    "sugg": e["new_text"],
                }
            )
        else:  # delete
            diffs.append(
                {
                    "block_id": bid,
                    "op": "delete",
                    "orig": text_by_id.get(bid, ""),
                    "sugg": None,
                }
            )

    for ins in insertions:
        # 契约统一用 block_id：op=insert_after 时它的语义是锚点块（插在其后）。
        diffs.append(
            {
                "block_id": ins["after_block_id"],
                "op": "insert_after",
                "orig": None,
                "sugg": ins["new_text"],
            }
        )

    return diffs


# ─────────────────────────────────────────────────────────────────────────────
# 端点
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/sessions/{session_id}/canvases/{canvas_id}/edit")
async def canvas_v2_edit(
    session_id: str,
    canvas_id: str,
    request: Request,
    _auth: tuple = Depends(require_auth()),
) -> JSONResponse:
    """作用域编辑：拉当前版本 → 组 prompt → gateway 结构化输出 → 落建议。

    请求体见 ``CanvasV2EditRequest``。成功 200 返回 ``{"suggestion": {...}}``
    （orchestrator 落库返回体）。失败走 problem+json，带中文 ``user_message``。
    """
    tenant_id, user_id, access_token = _auth
    settings = request.app.state.settings
    gateway_url: str = settings.foundation_llm_gateway_url
    orch_url: str = settings.session_orchestrator_url

    raw = await request.json()
    try:
        req = CanvasV2EditRequest.model_validate(raw)
    except Exception:  # noqa: BLE001
        raise _problem(
            400,
            "修订请求格式不正确，请检查后重试。",
            code="AGENT.BFF.CANVAS.BAD_REQUEST",
            detail="invalid canvas v2 edit request body",
        ) from None

    if not req.instruction.strip():
        raise _problem(
            400,
            "请填写修改要求后再提交。",
            code="AGENT.BFF.CANVAS.NO_INSTRUCTION",
            detail="instruction is required",
        )

    if req.scope.type == "blocks" and not (req.scope.block_ids or []):
        raise _problem(
            400,
            "请先选择要修改的段落。",
            code="AGENT.BFF.CANVAS.NO_BLOCK_IDS",
            detail="scope=blocks requires non-empty block_ids",
        )

    orch_headers = foundation_headers(tenant_id, user_id, access_token)

    # ── ① 拉当前版本 blocks（orchestrator 是 system of record）──────────────────
    async with httpx.AsyncClient(timeout=_META_TIMEOUT) as orch_client:
        try:
            canvas_resp = await orch_client.get(
                f"{orch_url}/canvases/{canvas_id}",
                headers=orch_headers,
            )
        except httpx.HTTPError as exc:
            logger.warning("canvas v2 edit: orchestrator unreachable (GET canvas): %s", exc)
            raise _problem(
                502,
                "画布服务暂时不可用，请稍后重试。",
                code="AGENT.BFF.CANVAS.ORCH_UNAVAILABLE",
                detail=f"orchestrator unreachable: {exc}",
            ) from exc

    if canvas_resp.status_code == 404:
        raise _problem(
            404,
            "画布不存在或已被删除。",
            code="AGENT.BFF.CANVAS.NOT_FOUND",
            detail="orchestrator returned 404 for canvas",
        )
    if canvas_resp.status_code != 200:
        raise _problem(
            502,
            "读取画布失败，请稍后重试。",
            code="AGENT.BFF.CANVAS.ORCH_ERROR",
            detail=f"orchestrator GET canvas returned HTTP {canvas_resp.status_code}",
        )

    try:
        canvas = canvas_resp.json()
        blocks = canvas["blocks"]
        base_version = canvas["current_version"]
        if not isinstance(blocks, list) or not isinstance(base_version, int):
            raise ValueError("unexpected canvas shape")
    except (ValueError, KeyError, TypeError) as exc:
        logger.warning("canvas v2 edit: bad canvas payload from orchestrator: %s", exc)
        raise _problem(
            502,
            "读取画布失败，请稍后重试。",
            code="AGENT.BFF.CANVAS.ORCH_BAD_PAYLOAD",
            detail="unexpected canvas detail response shape",
        ) from exc

    # ── 乐观锁前置校验：客户端显示的版本与服务端当前版本不一致，直接 409，
    #    不解析虚拟密钥、不调 LLM gateway。req.base_version 为 None 时行为不变。──
    if req.base_version is not None and req.base_version != base_version:
        raise _problem(
            409,
            "画布已被更新，请刷新后再修改。",
            code="AGENT.BFF.CANVAS.VERSION_CONFLICT",
            detail=f"client base_version {req.base_version} != current {base_version}",
        )

    # ── 作用域：blocks 时校验 block_ids 都存在；document 时目标 = 全部块 ──────────
    existing_ids = {b["id"] for b in blocks if isinstance(b, dict) and "id" in b}
    if req.scope.type == "blocks":
        target_ids = list(req.scope.block_ids or [])
        missing = [bid for bid in target_ids if bid not in existing_ids]
        if missing:
            raise _problem(
                422,
                "选中的段落已不在当前画布版本中，请刷新后重试。",
                code="AGENT.BFF.CANVAS.BLOCK_NOT_FOUND",
                detail=f"block_ids not in current version: {missing}",
            )
    else:  # document
        target_ids = [b["id"] for b in blocks if isinstance(b, dict) and "id" in b]

    if not target_ids:
        raise _problem(
            422,
            "当前画布没有可编辑的内容。",
            code="AGENT.BFF.CANVAS.EMPTY_CANVAS",
            detail="canvas has no blocks to edit",
        )

    # ── 虚拟密钥 + 模型解析（复用 canvas.py 基建）────────────────────────────────
    async with httpx.AsyncClient(timeout=_META_TIMEOUT) as meta_client:
        virtual_key = await _resolve_virtual_key(
            client=meta_client,
            gateway_url=gateway_url,
            tenant_id=tenant_id,
            access_token=access_token,
        )
        if not virtual_key:
            raise _problem(
                400,
                "当前租户尚未配置可用的模型密钥，请联系管理员在模型管理中配置。",
                code="AGENT.BFF.CANVAS.NO_VIRTUAL_KEY",
                detail="tenant has no enabled virtual key",
            )

        model = req.model or None
        if not model:
            model = await _resolve_default_model(
                client=meta_client,
                gateway_url=gateway_url,
                tenant_id=tenant_id,
                user_id=user_id,
                access_token=access_token,
            )
        if not model:
            raise _problem(
                400,
                "未指定模型，且当前租户没有配置默认模型，请在会话中选择一个模型后重试。",
                code="AGENT.BFF.CANVAS.NO_MODEL",
                detail="no model provided and tenant has no default model",
            )

    # ── ②③ 组 prompt → gateway 结构化输出（不合法重试一次）──────────────────────
    gw_headers = foundation_headers(tenant_id, user_id, access_token)
    gw_headers["X-LLM-Virtual-Key"] = virtual_key

    edits: list[dict] | None = None
    insertions: list[dict] | None = None
    change_summary: str | None = None
    retry_error: str | None = None

    async with httpx.AsyncClient(timeout=_COMPLETION_TIMEOUT) as gw_client:
        for attempt in range(2):  # 首次 + 一次重试
            prompt = build_scoped_edit_prompt(
                instruction=req.instruction,
                blocks=blocks,
                target_ids=target_ids,
                retry_error=retry_error,
            )
            payload = {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
                "response_format": {"type": "json_object"},
            }
            try:
                resp = await gw_client.post(
                    f"{gateway_url.rstrip('/')}/chat/completions",
                    json=payload,
                    headers=gw_headers,
                )
            except httpx.TimeoutException as exc:
                logger.warning("canvas v2 edit gateway timeout (session %s): %s", session_id, exc)
                raise _problem(
                    502,
                    "模型响应超时，请稍后重试。",
                    code="AGENT.BFF.CANVAS.GATEWAY_TIMEOUT",
                    detail="llm-gateway /chat/completions timed out",
                ) from exc
            except httpx.HTTPError as exc:
                logger.warning("canvas v2 edit gateway error (session %s): %s", session_id, exc)
                raise _problem(
                    502,
                    "模型服务暂时不可用，请稍后重试。",
                    code="AGENT.BFF.CANVAS.GATEWAY_UNAVAILABLE",
                    detail=f"llm-gateway unreachable: {exc}",
                ) from exc

            if resp.status_code != 200:
                logger.warning(
                    "canvas v2 edit gateway HTTP %s (session %s)",
                    resp.status_code,
                    session_id,
                )
                upstream_user_message: str | None = None
                try:
                    body = resp.json()
                    if isinstance(body, dict):
                        um = body.get("user_message")
                        if isinstance(um, str) and um:
                            upstream_user_message = um
                except Exception:  # noqa: BLE001
                    pass
                raise _problem(
                    502,
                    upstream_user_message or "模型服务返回错误，请稍后重试。",
                    code="AGENT.BFF.CANVAS.GATEWAY_ERROR",
                    detail=f"llm-gateway returned HTTP {resp.status_code}",
                )

            try:
                data = resp.json()
                content = data["choices"][0]["message"]["content"]
                if not isinstance(content, str):
                    content = str(content)
            except Exception as exc:  # noqa: BLE001
                logger.warning("canvas v2 edit: failed to parse gateway completion: %s", exc)
                raise _problem(
                    502,
                    "模型返回内容无法解析，请重试。",
                    code="AGENT.BFF.CANVAS.BAD_COMPLETION",
                    detail="unexpected chat/completions response shape",
                ) from exc

            try:
                edits, insertions, change_summary = parse_and_validate_output(
                    content, target_ids=target_ids
                )
                break
            except ModelOutputError as exc:
                retry_error = str(exc)
                logger.info(
                    "canvas v2 edit: model output invalid (attempt %s): %s",
                    attempt + 1,
                    retry_error,
                )
                # 循环再来一次；两次都失败落到下面的 502。
                continue

    if edits is None:
        raise _problem(
            502,
            "模型未能给出可用的修改建议，请调整指令后重试。",
            code="AGENT.BFF.CANVAS.BAD_MODEL_OUTPUT",
            detail=f"model output failed schema validation after retry: {retry_error}",
        )

    # ── ④⑤ 换算 diffs → POST 建议落库（orchestrator）────────────────────────────
    diffs = build_diffs(edits=edits, insertions=insertions or [], blocks=blocks)
    suggestion_body = {
        "base_version": base_version,
        "instruction": req.instruction,
        "diffs": diffs,
        "change_summary": change_summary,
    }

    async with httpx.AsyncClient(timeout=_META_TIMEOUT) as orch_client:
        try:
            sugg_resp = await orch_client.post(
                f"{orch_url}/canvases/{canvas_id}/suggestions",
                json=suggestion_body,
                headers=orch_headers,
            )
        except httpx.HTTPError as exc:
            logger.warning("canvas v2 edit: orchestrator unreachable (POST suggestion): %s", exc)
            raise _problem(
                502,
                "保存修改建议失败，请稍后重试。",
                code="AGENT.BFF.CANVAS.ORCH_UNAVAILABLE",
                detail=f"orchestrator unreachable: {exc}",
            ) from exc

    if sugg_resp.status_code == 404:
        raise _problem(
            404,
            "画布不存在或已被删除。",
            code="AGENT.BFF.CANVAS.NOT_FOUND",
            detail="orchestrator returned 404 for suggestion",
        )
    if sugg_resp.status_code == 409:
        upstream_user_message = None
        try:
            body = sugg_resp.json()
            if isinstance(body, dict):
                um = body.get("user_message") or (
                    body.get("detail", {}).get("user_message")
                    if isinstance(body.get("detail"), dict)
                    else None
                )
                if isinstance(um, str) and um:
                    upstream_user_message = um
        except Exception:  # noqa: BLE001
            pass
        raise _problem(
            409,
            upstream_user_message or "画布已被他人更新，请刷新后重新提交修改。",
            code="AGENT.BFF.CANVAS.VERSION_CONFLICT",
            detail="orchestrator returned 409 (base_version conflict)",
        )
    if sugg_resp.status_code not in (200, 201):
        raise _problem(
            502,
            "保存修改建议失败，请稍后重试。",
            code="AGENT.BFF.CANVAS.ORCH_ERROR",
            detail=f"orchestrator POST suggestion returned HTTP {sugg_resp.status_code}",
        )

    try:
        suggestion = sugg_resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("canvas v2 edit: bad suggestion payload from orchestrator: %s", exc)
        raise _problem(
            502,
            "保存修改建议失败，请稍后重试。",
            code="AGENT.BFF.CANVAS.ORCH_BAD_PAYLOAD",
            detail="unexpected suggestion response shape",
        ) from exc

    return JSONResponse(status_code=200, content={"suggestion": suggestion})
