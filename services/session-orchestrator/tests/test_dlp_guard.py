"""Unit tests for the foundation-DLP client (dlp_guard.py).

check_download:
  * allowed=false                 → raises DlpDownloadDeniedError (with reason)
  * allowed=true                  → returns (no raise)
  * network error / timeout       → fail-OPEN (returns, no raise)
  * non-200                       → fail-OPEN (returns, no raise)

scan_and_tag:
  * action=ALLOW                  → no classification:tag call
  * action!=ALLOW + sensitivity   → classification:tag called with sensitivity
  * network error                 → fail-OPEN (no raise)
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from agent_session_orchestrator.api import dlp_guard
from agent_session_orchestrator.api.dlp_guard import (
    DlpDownloadDeniedError,
    check_download,
    scan_and_tag,
)

_DLP_CLIENT = "agent_session_orchestrator.api.dlp_guard.httpx.AsyncClient"
_TENANT = "tnt_test"
_USER = "usr_test"


def _resp(status_code: int = 200, body: dict[str, Any] | None = None) -> MagicMock:
    r = MagicMock(spec=httpx.Response)
    r.status_code = status_code
    r.json = MagicMock(return_value=body or {})
    return r


def _client_ctx(post: AsyncMock) -> MagicMock:
    """Build an AsyncClient context-manager mock whose .post is ``post``."""
    instance = AsyncMock()
    instance.post = post
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=instance)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


# --------------------------------------------------------------------------- #
# check_download
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_check_download_denied_raises() -> None:
    post = AsyncMock(return_value=_resp(200, {"allowed": False, "reason": "L5 to laptop"}))
    with (
        patch(_DLP_CLIENT, return_value=_client_ctx(post)),
        pytest.raises(DlpDownloadDeniedError) as ei,
    ):
        await check_download(
            dlp_url="http://dlp", tenant_id=_TENANT, user_id=_USER,
            resource_id="att_1",
        )
    assert "L5 to laptop" in ei.value.reason


@pytest.mark.asyncio
async def test_check_download_allowed_passes() -> None:
    post = AsyncMock(return_value=_resp(200, {"allowed": True}))
    with patch(_DLP_CLIENT, return_value=_client_ctx(post)):
        await check_download(
            dlp_url="http://dlp", tenant_id=_TENANT, user_id=_USER,
            resource_id="att_1",
        )
    # X-Tenant-Id forwarded as header, tenant not in body.
    kw = post.call_args.kwargs
    assert kw["headers"]["X-Tenant-Id"] == _TENANT
    assert "tenant_id" not in kw["json"]
    assert kw["json"]["destination"] == "laptop"
    assert kw["json"]["user_clearance"] == "L2"  # conservative fallback default


@pytest.mark.asyncio
async def test_check_download_network_error_fail_open() -> None:
    post = AsyncMock(side_effect=httpx.ConnectError("refused"))
    with patch(_DLP_CLIENT, return_value=_client_ctx(post)):
        # Must NOT raise — fail-open.
        await check_download(
            dlp_url="http://dlp", tenant_id=_TENANT, user_id=_USER,
            resource_id="att_1",
        )


@pytest.mark.asyncio
async def test_check_download_non_200_fail_open() -> None:
    post = AsyncMock(return_value=_resp(503, {}))
    with patch(_DLP_CLIENT, return_value=_client_ctx(post)):
        await check_download(
            dlp_url="http://dlp", tenant_id=_TENANT, user_id=_USER,
            resource_id="att_1",
        )


# --------------------------------------------------------------------------- #
# scan_and_tag
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_scan_allow_no_tag() -> None:
    post = AsyncMock(return_value=_resp(200, {"action": "ALLOW", "max_sensitivity": None}))
    with patch(_DLP_CLIENT, return_value=_client_ctx(post)):
        await scan_and_tag(
            dlp_url="http://dlp", tenant_id=_TENANT,
            content="hello", resource_id="att_1",
        )
    # Only the scan call, no classification:tag.
    assert post.call_count == 1
    assert "/core/scan" in post.call_args_list[0].args[0]


@pytest.mark.asyncio
async def test_scan_hit_tags_with_sensitivity() -> None:
    calls: list[str] = []

    async def _post(url: str, **kw: Any) -> MagicMock:
        calls.append(url)
        if url.endswith("/core/scan"):
            return _resp(200, {"action": "QUARANTINE", "max_sensitivity": "L4", "hits": [1]})
        return _resp(200, {})

    post = AsyncMock(side_effect=_post)
    with patch(_DLP_CLIENT, return_value=_client_ctx(post)):
        await scan_and_tag(
            dlp_url="http://dlp", tenant_id=_TENANT,
            content="secret", resource_id="att_1",
        )
    assert any(u.endswith("/core/classification:tag") for u in calls)
    tag_call = next(c for c in post.call_args_list if c.args[0].endswith("classification:tag"))
    assert tag_call.kwargs["json"]["sensitivity"] == "L4"
    assert tag_call.kwargs["json"]["reason"] == "auto by upload scan"
    assert tag_call.kwargs["headers"]["X-Tenant-Id"] == _TENANT


@pytest.mark.asyncio
async def test_scan_network_error_fail_open() -> None:
    post = AsyncMock(side_effect=httpx.TimeoutException("slow"))
    with patch(_DLP_CLIENT, return_value=_client_ctx(post)):
        # Must NOT raise.
        await scan_and_tag(
            dlp_url="http://dlp", tenant_id=_TENANT,
            content="x", resource_id="att_1",
        )


def test_default_clearance_constant() -> None:
    assert dlp_guard._DEFAULT_CLEARANCE == "L2"
