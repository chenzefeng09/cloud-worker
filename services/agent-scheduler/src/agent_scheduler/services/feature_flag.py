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
