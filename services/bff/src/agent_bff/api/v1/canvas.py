"""Canvas局部修订端点 —— BFF 直连 llm-gateway 做一次 chat completion。

产品的画布（canvas）功能里，用户选中文章的一段文字提出修改要求。旧路径是
前端把要求拼成大 prompt 走普通聊天通道（起完整 sandbox run、落聊天消息），
用户能看到这些中间消息。本端点把它改成一次轻量的、无副作用的修订：

  * **不建 run、不写 message 表、不调 orchestrator。**
  * BFF 服务端拼接编辑 prompt（语义对齐前端
    ``apps/web/src/lib/canvasObjects.ts`` 的 ``buildCanvasEditPrompt``），
    直连 llm-gateway ``POST /chat/completions``（OpenAI 协议），
    解析回复里的 ``<canvas-replacement>`` / ``<change-summary>`` 标签，
    直接把替换文本返回给前端。

错误处理遵守本仓 problem+json 惯例：``detail`` 里放结构化 problem，
带 ``user_message``（中文、用户可读）；不把上游整坨 JSON 原样透传。
"""

from __future__ import annotations

import logging
import re
import time

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator

from agent_bff.api._http import foundation_headers
from agent_bff.api.dependencies import require_auth

router = APIRouter(tags=["Canvas"])
logger = logging.getLogger(__name__)

# gateway ``/chat/completions`` 超时（秒）。推理模型思考阶段可能很长，给足 300s。
_COMPLETION_TIMEOUT = 300.0
# tenant-keys / tenant-defaults 这类轻量元数据查询用短超时。
_META_TIMEOUT = 5.0

# 进程内虚拟密钥缓存：{tenant_id: (virtual_key, expires_ts)}，TTL 300s。
# 避免每次修订都查一遍 gateway ``/tenant-keys``。
_VKEY_TTL_SECONDS = 300.0
_vkey_cache: dict[str, tuple[str, float]] = {}


# ─────────────────────────────────────────────────────────────────────────────
# 请求模型
# ─────────────────────────────────────────────────────────────────────────────

class _Selection(BaseModel):
    text: str
    start: int
    end: int


class CanvasEditRequest(BaseModel):
    """``POST /sessions/{id}/canvas/edit`` 请求体。

    契约见任务描述；``extra="allow"`` 容忍前端多传字段（如 kind/documentId），
    BFF 只取拼 prompt 需要的几项。
    """

    model_config = {"extra": "allow"}

    title: str = ""
    content: str = ""
    scope: str = "selection"
    selection: _Selection | None = None
    instruction: str = ""
    model: str | None = None

    @field_validator("scope")
    @classmethod
    def _validate_scope(cls, v: str) -> str:
        if v not in ("selection", "document"):
            raise ValueError("scope must be 'selection' or 'document'")
        return v


# ─────────────────────────────────────────────────────────────────────────────
# problem+json 帮手
# ─────────────────────────────────────────────────────────────────────────────

def _problem(status: int, user_message: str, *, code: str, detail: str) -> HTTPException:
    """构造带 ``user_message`` 的 problem+json HTTPException。

    结构与本仓 ``_raise_for_upstream`` 一致：``detail`` 内嵌结构化 problem，
    同级放 ``user_message`` 供前端直接提取。绝不把上游整坨 JSON 原样透传。
    """
    return HTTPException(
        status_code=status,
        detail={
            "detail": {"code": code, "detail": detail},
            "user_message": user_message,
        },
    )


# ─────────────────────────────────────────────────────────────────────────────
# 编辑 prompt 拼接（语义对齐前端 buildCanvasEditPrompt）
# ─────────────────────────────────────────────────────────────────────────────

def build_canvas_edit_prompt(req: CanvasEditRequest) -> str:
    """服务端拼接编辑 prompt。

    语义对齐前端 ``canvasObjects.ts`` 的 ``buildCanvasEditPrompt``：
    专业编辑角色、标题、修改范围（selection/document）、选中片段附字符区间、
    要求严格按 ``<canvas-replacement>`` + ``<change-summary>`` 格式返回、
    标签内不放解释。

    与前端的唯一差异：前端按 ``kind`` 取对象 label（文章/报告/…），BFF 侧
    不透传 kind 也不复制那张枚举表，统一用中性词「文档」，不影响修订质量。
    """
    label = "文档"
    if req.scope == "selection" and req.selection is not None:
        target = (
            f"选中片段（字符 {req.selection.start}-{req.selection.end}）：\n"
            f"{req.selection.text}"
        )
    else:
        target = f"整篇{label}：\n{req.content}"

    title = req.title or f"未命名{label}"
    scope_desc = "只修改选中片段" if req.scope == "selection" else "整篇修改"

    if req.scope == "selection":
        replacement_hint = "这里放【只替换选中片段】的改写结果"
        format_rules = [
            "请严格按下面格式返回：",
            "- <canvas-replacement> 内只能是选中片段的改写结果，",
            "  用它原地替换选中片段即可拼回完整文档。",
            "- 禁止在 <canvas-replacement> 内包含选区之外的任何原文，",
            "  禁止返回整篇全文或选区所在的整段上下文，禁止回显选区前后的句子。",
            "- 不要在 <canvas-replacement> 内放解释。",
        ]
    else:
        replacement_hint = "这里放可直接替换整篇的文本"
        format_rules = [
            "请严格按下面格式返回，不要在 <canvas-replacement> 内放解释：",
        ]

    return "\n".join(
        [
            f"请作为专业编辑，帮我修改下面这份{label}。",
            f"标题：{title}",
            f"修改范围：{scope_desc}",
            f"修改要求：{req.instruction}",
            "",
            target,
            "",
            *format_rules,
            "<canvas-replacement>",
            replacement_hint,
            "</canvas-replacement>",
            "<change-summary>",
            "- 这里列出关键改动",
            "</change-summary>",
        ]
    )


# ─────────────────────────────────────────────────────────────────────────────
# 响应解析（对齐前端 extractCanvasReplacement）
# ─────────────────────────────────────────────────────────────────────────────

_REPLACEMENT_RE = re.compile(
    r"<canvas-replacement>\s*([\s\S]*?)\s*</canvas-replacement>", re.IGNORECASE
)
_SUMMARY_RE = re.compile(
    r"<change-summary>\s*([\s\S]*?)\s*</change-summary>", re.IGNORECASE
)
# 剥掉最外层可能的 markdown 代码围栏（```lang\n…\n```）。
_FENCE_RE = re.compile(r"^```(?:\w+)?\n([\s\S]*?)\n```$")


def _strip_outer_fence(text: str) -> str:
    """剥掉替换文本最外层的 markdown 代码围栏。对齐前端逻辑。"""
    stripped = text.strip()
    m = _FENCE_RE.match(stripped)
    return m.group(1).strip() if m else stripped


def parse_canvas_response(content: str) -> tuple[str, str | None]:
    """解析模型回复，返回 ``(replacement, summary)``。

    * ``<canvas-replacement>...</canvas-replacement>``：大小写不敏感，
      容忍标签外多余文字，剥掉替换文本外层可能的代码围栏。
    * ``<change-summary>...</change-summary>``：同上；无则 summary 为 None。
    * 无 replacement 标签的兜底：整段回复去掉 summary 标签后作为 replacement。

    replacement 恒为字符串（可能为空，由调用方判空后转 502）。
    """
    summary_match = _SUMMARY_RE.search(content)
    summary = summary_match.group(1).strip() if summary_match else None
    if summary == "":
        summary = None

    replacement_match = _REPLACEMENT_RE.search(content)
    if replacement_match:
        replacement = _strip_outer_fence(replacement_match.group(1))
    else:
        # 兜底：整段回复去掉 summary 标签后作为 replacement。
        fallback = _SUMMARY_RE.sub("", content).strip()
        replacement = _strip_outer_fence(fallback)

    return replacement, summary


# ─────────────────────────────────────────────────────────────────────────────
# replacement 归一化（选区修订的位置无关兜底）
# ─────────────────────────────────────────────────────────────────────────────
#
# 模型常不守约：让它只改「选中片段」，它却把选区所在整段、带前后半句原文的一
# 段、甚至整篇改写后的全文塞进 <canvas-replacement>。链路按「片段替换」在选区
# 位置拼接（prefix + replacement + suffix），于是选区之外的原文被重复。
#
# 这里做与「模型把什么塞进标签」无关的归一化：拿选区两侧的原文（prefix/suffix）
# 去和 replacement 的首尾求最长公共串，把回显的原文剪掉。该算法天然覆盖：
#   * 返回完整全文        → 头部重叠=整个 prefix、尾部重叠=整个 suffix，剪完剩中间；
#   * 返回整段落/带半句   → 剪掉局部重叠；
#   * 文末/文首选区       → 一侧为空只剪另一侧。
# 剪不动时再用「指纹 + 长度」兜底识别整篇重写，改走整篇替换。

# 判定回显所需的最小重叠长度（字符）。太短会误剪守约改写里的巧合公共串。
_MIN_OVERLAP = 10
# 整篇重写检测取的开头指纹长度（非空白字符数）。
_FINGERPRINT_LEN = 60


def _normalize_ws(text: str) -> str:
    """把连续空白（含换行）归一为单个空格，用于容忍空白差异的比较。"""
    return re.sub(r"\s+", " ", text)


def _longest_suffix_prefix_overlap(left: str, right: str) -> int:
    """求 ``left`` 的后缀与 ``right`` 的前缀的最长公共串长度（按 right 的字符计）。

    比较容忍空白差异：两侧在边界处的空白归一后逐长度比对（strip 掉边界空白，
    这样画布里的 ``\\n\\n`` 与 replacement 里的单空格也算匹配）。返回值是
    ``right`` 里实际被覆盖的字符数，供调用方按 replacement 真实字符切片。
    """
    if not left or not right:
        return 0
    max_len = min(len(left), len(right))
    best = 0
    for length in range(1, max_len + 1):
        right_head = _normalize_ws(right[:length]).strip()
        if not right_head:
            continue
        # left 的后缀取足够长的一段来容纳（考虑空白差异，多取一些余量）。
        left_tail = _normalize_ws(left[-(length * 2 + 8):]).strip()
        if left_tail.endswith(right_head):
            best = length
    return best


def _longest_prefix_suffix_overlap(right: str, left: str) -> int:
    """求 ``right`` 的前缀与 ``left`` 的后缀的最长公共串长度（按 right 的字符计）。

    用于尾部：``right`` = suffix（选区之后的原文），``left`` = replacement，
    求 replacement 结尾回显了多少 suffix 开头。返回 replacement 里被覆盖的字符数。
    比较同样容忍边界空白差异。
    """
    if not left or not right:
        return 0
    max_len = min(len(left), len(right))
    best = 0
    for length in range(1, max_len + 1):
        left_tail = _normalize_ws(left[-length:]).strip()
        if not left_tail:
            continue
        right_head = _normalize_ws(right[: length * 2 + 8]).strip()
        if right_head.startswith(left_tail):
            best = length
    return best


def _fingerprint(text: str, n: int) -> str:
    """取 ``text`` 开头 ``n`` 个非空白字符作为指纹。"""
    out: list[str] = []
    for ch in text:
        if ch.isspace():
            continue
        out.append(ch)
        if len(out) >= n:
            break
    return "".join(out)


def normalize_selection_replacement(
    replacement: str, content: str, start: int, end: int
) -> tuple[str, str]:
    """归一化选区修订的 replacement，返回 ``(replacement, apply_to)``。

    * ``apply_to == "selection"``：replacement 已是「只替换选中片段」的净片段，
      调用方原地替换选区。
    * ``apply_to == "document"``：识别出模型返回了整篇重写，replacement 原样
      作为整篇新内容，调用方整篇替换。

    仅在 ``start/end`` 合法（``0 <= start <= end <= len(content)``）时生效，
    否则原样返回 selection。剪后为空/全空白由调用方判空转 502。
    """
    if not (0 <= start <= end <= len(content)):
        return replacement, "selection"

    prefix = content[:start]
    suffix = content[end:]

    # a) 头部重叠修剪：prefix 后缀 vs replacement 前缀。
    head_overlap = _longest_suffix_prefix_overlap(prefix, replacement)
    trimmed = replacement[head_overlap:] if head_overlap >= _MIN_OVERLAP else replacement

    # a) 尾部重叠修剪：suffix 前缀 vs (剪过头部的) replacement 后缀。
    tail_overlap = _longest_prefix_suffix_overlap(suffix, trimmed)
    if tail_overlap >= _MIN_OVERLAP:
        trimmed = trimmed[: len(trimmed) - tail_overlap]

    trimmed_any = (head_overlap >= _MIN_OVERLAP) or (tail_overlap >= _MIN_OVERLAP)
    if trimmed_any:
        # 剪出了东西：即使剪成空白，也交给调用方判空转 502（模型未返回有效修订）。
        return trimmed, "selection"

    # b) 整篇重写检测（剪不出东西时的降级兜底）。
    fp = _fingerprint(content, _FINGERPRINT_LEN)
    if fp:
        repl_fp_probe = _normalize_ws(replacement)
        content_fp_norm = _normalize_ws(fp)
        selection_span = max(end - start, 0)
        if (
            content_fp_norm
            and content_fp_norm in repl_fp_probe
            and len(replacement) > max(200, 2 * selection_span)
        ):
            return replacement, "document"

    # c) 其余情况：守约的净片段，原样 selection。
    return replacement, "selection"


# ─────────────────────────────────────────────────────────────────────────────
# 虚拟密钥获取（照抄 scheduler _fetch_tenant_virtual_key 范式 + 进程内缓存）
# ─────────────────────────────────────────────────────────────────────────────

async def _fetch_tenant_virtual_key(
    *,
    client: httpx.AsyncClient,
    gateway_url: str,
    tenant_id: str,
    access_token: str,
) -> str | None:
    """查询 gateway 的租户虚拟密钥（GET /tenant-keys）。

    返回第一个 enabled key 的 ``virtual_key``（容忍 ``key`` / ``value`` 别名），
    拿不到或 gateway 出错则返回 None（由调用方转 problem+json）。
    照抄 scheduler ``_fetch_tenant_virtual_key`` 的解析范式。
    """
    url = f"{gateway_url.rstrip('/')}/tenant-keys"
    headers = foundation_headers(tenant_id, "", access_token)
    try:
        resp = await client.get(url, params={"tenant_id": tenant_id}, headers=headers)
    except httpx.HTTPError as exc:
        logger.warning("llm-gateway unreachable while fetching tenant virtual key: %s", exc)
        return None

    if resp.status_code != 200:
        logger.warning("llm-gateway returned HTTP %s for GET /tenant-keys", resp.status_code)
        return None

    def _pick(item: dict) -> str | None:
        if not item.get("enabled", True):
            return None
        val = item.get("virtual_key") or item.get("key") or item.get("value")
        return str(val) if val else None

    try:
        keys = resp.json()
        if isinstance(keys, list):
            for item in keys:
                if isinstance(item, dict) and (val := _pick(item)):
                    return val
        elif isinstance(keys, dict):
            for field in ("keys", "data", "items", "results"):
                items = keys.get(field, [])
                if isinstance(items, list):
                    for item in items:
                        if isinstance(item, dict) and (val := _pick(item)):
                            return val
    except Exception as exc:  # noqa: BLE001
        logger.warning("failed to parse /tenant-keys response: %s", exc)
    return None


async def _resolve_virtual_key(
    *,
    client: httpx.AsyncClient,
    gateway_url: str,
    tenant_id: str,
    access_token: str,
) -> str | None:
    """带进程内缓存（TTL 300s）地拿虚拟密钥。缓存未命中再查 gateway。"""
    now = time.monotonic()
    cached = _vkey_cache.get(tenant_id)
    if cached is not None and cached[1] > now:
        return cached[0]

    key = await _fetch_tenant_virtual_key(
        client=client,
        gateway_url=gateway_url,
        tenant_id=tenant_id,
        access_token=access_token,
    )
    if key:
        _vkey_cache[tenant_id] = (key, now + _VKEY_TTL_SECONDS)
    return key


# ─────────────────────────────────────────────────────────────────────────────
# 默认模型解析
# ─────────────────────────────────────────────────────────────────────────────

async def _resolve_default_model(
    *,
    client: httpx.AsyncClient,
    gateway_url: str,
    tenant_id: str,
    user_id: str,
    access_token: str,
) -> str | None:
    """请求未传 model 时，查 gateway 租户默认模型。

    调 ``GET /tenant-defaults/resolve?purpose=llm``（BFF ``admin_model.py`` 里
    tenant-defaults 代理用的同一 gateway 路径），取 ``model_id``。
    拿不到则返回 None（调用方转 4xx，让前端补传 model）。
    """
    url = f"{gateway_url.rstrip('/')}/tenant-defaults/resolve"
    headers = foundation_headers(tenant_id, user_id, access_token)
    try:
        resp = await client.get(url, params={"purpose": "llm"}, headers=headers)
    except httpx.HTTPError as exc:
        logger.warning("llm-gateway unreachable while resolving default model: %s", exc)
        return None
    if resp.status_code != 200:
        logger.warning(
            "llm-gateway returned HTTP %s for GET /tenant-defaults/resolve", resp.status_code
        )
        return None
    try:
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("failed to parse /tenant-defaults/resolve response: %s", exc)
        return None
    if isinstance(data, dict):
        model_id = data.get("model_id")
        if model_id:
            return str(model_id)
    return None


# ─────────────────────────────────────────────────────────────────────────────
# 端点
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/sessions/{session_id}/canvas/edit")
async def canvas_edit(
    session_id: str,
    request: Request,
    _auth: tuple = Depends(require_auth()),
) -> JSONResponse:
    """画布局部修订：直连 llm-gateway 做一次 chat completion，不落消息、不建 run。

    请求体见 ``CanvasEditRequest``。成功 200 返回
    ``{"replacement": "...", "summary": "..." | null}``。
    失败走 problem+json，带中文 ``user_message``。
    """
    tenant_id, user_id, access_token = _auth
    settings = request.app.state.settings
    gateway_url: str = settings.foundation_llm_gateway_url

    raw = await request.json()
    try:
        req = CanvasEditRequest.model_validate(raw)
    except Exception:  # noqa: BLE001
        raise _problem(
            400,
            "修订请求格式不正确，请检查后重试。",
            code="AGENT.BFF.CANVAS.BAD_REQUEST",
            detail="invalid canvas edit request body",
        ) from None

    if not req.instruction.strip():
        raise _problem(
            400,
            "请填写修改要求后再提交。",
            code="AGENT.BFF.CANVAS.NO_INSTRUCTION",
            detail="instruction is required",
        )

    prompt = build_canvas_edit_prompt(req)

    async with httpx.AsyncClient(timeout=_META_TIMEOUT) as meta_client:
        # 1. 虚拟密钥（gateway /chat/completions 强制要求 X-LLM-Virtual-Key）。
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

        # 2. 模型：请求传了就用；没传则查租户默认模型；仍无则 4xx。
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

    # 3. 调 gateway /chat/completions（OpenAI 协议）。
    headers = foundation_headers(tenant_id, user_id, access_token)
    headers["X-LLM-Virtual-Key"] = virtual_key
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
    }

    async with httpx.AsyncClient(timeout=_COMPLETION_TIMEOUT) as client:
        try:
            resp = await client.post(
                f"{gateway_url.rstrip('/')}/chat/completions",
                json=payload,
                headers=headers,
            )
        except httpx.TimeoutException as exc:
            logger.warning("canvas edit gateway timeout for session %s: %s", session_id, exc)
            raise _problem(
                502,
                "模型响应超时，请稍后重试。",
                code="AGENT.BFF.CANVAS.GATEWAY_TIMEOUT",
                detail="llm-gateway /chat/completions timed out",
            ) from exc
        except httpx.HTTPError as exc:
            logger.warning("canvas edit gateway error for session %s: %s", session_id, exc)
            raise _problem(
                502,
                "模型服务暂时不可用，请稍后重试。",
                code="AGENT.BFF.CANVAS.GATEWAY_UNAVAILABLE",
                detail=f"llm-gateway unreachable: {exc}",
            ) from exc

    if resp.status_code != 200:
        logger.warning(
            "canvas edit gateway HTTP %s for session %s", resp.status_code, session_id
        )
        # 尝试透出上游的 user_message（若有），否则给通用中文提示。
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

    # 4. 解析 OpenAI 协议回复。
    try:
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        if not isinstance(content, str):
            content = str(content)
    except Exception as exc:  # noqa: BLE001
        logger.warning("canvas edit: failed to parse gateway completion: %s", exc)
        raise _problem(
            502,
            "模型返回内容无法解析，请重试。",
            code="AGENT.BFF.CANVAS.BAD_COMPLETION",
            detail="unexpected chat/completions response shape",
        ) from exc

    replacement, summary = parse_canvas_response(content)

    # scope=document 天然是整篇替换；scope=selection 走归一化兜底，防止模型把
    # 选区之外的原文/整篇全文塞进标签导致拼接后正文重复。
    apply_to = "document" if req.scope == "document" else "selection"
    if req.scope == "selection" and req.selection is not None:
        replacement, apply_to = normalize_selection_replacement(
            replacement,
            req.content,
            req.selection.start,
            req.selection.end,
        )

    if not replacement.strip():
        raise _problem(
            502,
            "模型未返回有效修订，请重试。",
            code="AGENT.BFF.CANVAS.EMPTY_REPLACEMENT",
            detail="model returned empty replacement",
        )

    return JSONResponse(
        status_code=200,
        content={"replacement": replacement, "summary": summary, "apply_to": apply_to},
    )
