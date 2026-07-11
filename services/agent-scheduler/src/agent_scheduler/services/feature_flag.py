"""Feature-flag evaluation — sync client for the foundation feature-flag service.

Phase 5 introduces a grey-release / rollback switch between the AgentScope
sandbox image and the new Claude SDK worker image.  ``spawn_run`` is
synchronous, so this client is deliberately sync (``httpx.Client``); we do not
introduce ``async`` into the spawn hot path.

Failure-open is a hard rule
---------------------------
The flag service is *advisory*: it only chooses which worker image to launch.
If it is slow, down, returns a non-200, or hands back a malformed body, we must
NOT fail the run.  Every error path logs a warning and returns ``False`` — i.e.
fall back to the existing AgentScope path.  A broken flag service must never
take run scheduling down with it.
"""

from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)


def evaluate_flag(
    flag: str,
    *,
    tenant_id: str,
    user_id: str,
    flag_url: str,
    timeout_ms: int = 200,
) -> bool:
    """Evaluate ``flag`` for the given tenant/user against the flag service.

    Args:
        flag: the flag name, e.g. ``"enable_claude_sdk_worker"``.
        tenant_id, user_id: evaluation context; also sent as
            ``X-Tenant-Id`` / ``X-User-Id`` headers (foundation convention).
        flag_url: base URL of the feature-flag service *with* the API version
            prefix (``settings.feature_flag_url``).  We POST to
            ``{flag_url}/evaluate``.
        timeout_ms: total request timeout in milliseconds.  Kept tight (200ms
            default) because this sits on the synchronous spawn path.

    Returns:
        The service's ``enabled`` boolean, or ``False`` on *any* error
        (connection, timeout, non-200, malformed body) — failure-open.
    """
    url = f"{flag_url.rstrip('/')}/evaluate"
    body = {
        "flag": flag,
        "context": {"tenant_id": tenant_id, "user_id": user_id},
    }
    headers = {"X-Tenant-Id": tenant_id, "X-User-Id": user_id}
    try:
        with httpx.Client(timeout=timeout_ms / 1000) as client:
            resp = client.post(url, json=body, headers=headers)
        if resp.status_code != 200:
            logger.warning(
                "feature_flag: evaluate returned non-200 (failure-open → False)",
                extra={"flag": flag, "status": resp.status_code, "tenant_id": tenant_id},
            )
            return False
        enabled = resp.json().get("enabled")
        return bool(enabled)
    except Exception as exc:  # noqa: BLE001 -- failure-open is the whole point
        logger.warning(
            "feature_flag: evaluate failed (failure-open → False)",
            extra={"flag": flag, "tenant_id": tenant_id, "err": str(exc)},
        )
        return False


# Extension name the ``canvas_tool`` package registers under (must match
# ``agentscope_ext.bootstrap._KNOWN``).
_CANVAS_TOOL_EXTENSION = "canvas_tool"


def runtime_extensions_for_run(
    *,
    base_extensions: str,
    tenant_id: str,
    user_id: str,
    flag_url: str,
    timeout_ms: int = 200,
) -> str:
    """Return the ``RUNTIME_EXTENSIONS`` value for a single run.

    Starts from the configured ``base_extensions`` and appends ``canvas_tool``
    when the per-run ``enable_canvas_tool`` feature flag is on (canvas v2, Phase
    B).  The flag is evaluated with the same failure-open ``evaluate_flag`` used
    for image routing: a down/slow/misconfigured flag service returns False, so
    the canvas tool is simply not appended — the run is byte-for-byte identical
    to the pre-canvas behaviour.

    Idempotent: if ``base_extensions`` already lists ``canvas_tool`` (e.g. a
    deployment hard-wired it) we never add a duplicate.
    """
    names = [part.strip() for part in base_extensions.split(",") if part.strip()]
    if _CANVAS_TOOL_EXTENSION in names:
        return base_extensions

    enabled = evaluate_flag(
        "enable_canvas_tool",
        tenant_id=tenant_id,
        user_id=user_id,
        flag_url=flag_url,
        timeout_ms=timeout_ms,
    )
    if not enabled:
        return base_extensions

    names.append(_CANVAS_TOOL_EXTENSION)
    return ",".join(names)
