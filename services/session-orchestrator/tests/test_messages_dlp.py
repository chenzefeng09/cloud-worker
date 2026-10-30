"""Output-side DLP tests for assistant-transcript finalization.

These exercise ``_reconcile_run_completion`` directly (it is a plain async
helper) rather than through the HTTP layer, so the assertions stay focused on
the DLP side-effect at the message-persist point — the natural, low-frequency
finalization hook (NOT the per-token stream).

Two behaviours are pinned:

* When a run finalizes and an assistant summary row is persisted,
  ``scan_and_tag`` is awaited with the message text + tenant (fail-open
  output-side scan, mirroring the attachment-upload path).
* A DLP failure does **not** break run finalization — the assistant message is
  still persisted and the helper returns terminal=True (fully fail-open).
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from agent_session_orchestrator.api.v1 import messages as messages_mod
from agent_session_orchestrator.api.v1.messages import _reconcile_run_completion
from agent_session_orchestrator.domain.models import Run
from tests._helpers import TEST_TENANT, TEST_USER

_SUMMARY = "Here is the report. Contact SSN 123-45-6789 if needed."


def _completed_run() -> Run:
    return Run(
        id="run_dlp1",
        tenant_id=TEST_TENANT,
        session_id="ses_dlp1",
        sandbox_id="sbx_abc123",
        task_description="t",
        status="RUNNING",
        created_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
    )


class _SchedulerCompletedClient:
    """Fake httpx.AsyncClient: scheduler GET /runs/{id} → COMPLETED + summary."""

    async def __aenter__(self) -> _SchedulerCompletedClient:
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def get(self, url: str, **kw: Any) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": "COMPLETED",
                "result_summary": _SUMMARY,
                "result_error": None,
                "artifacts": [],
                "usage": {},
                "plan_steps": [],
            },
        )


def _make_svc() -> MagicMock:
    """SessionService stand-in: create_message returns a Message-like row."""
    svc = MagicMock()

    async def _create_message(*args: Any, **kw: Any) -> MagicMock:
        msg = MagicMock()
        msg.id = "msg_persisted1"
        return msg

    svc.create_message = AsyncMock(side_effect=_create_message)
    svc.claim_run_transition = AsyncMock(return_value=True)
    svc.run_has_output = AsyncMock(return_value=False)
    svc.raise_run_usage = AsyncMock()
    # No redis_url is passed below, so the stream-replay branch (which lists
    # existing messages) is skipped; list_messages is never called.
    return svc


@pytest.mark.asyncio
async def test_assistant_finalization_scans_via_dlp() -> None:
    """When the summary row is persisted, scan_and_tag is awaited with text+tenant."""
    run = _completed_run()
    svc = _make_svc()

    with (
        patch.object(httpx, "AsyncClient", return_value=_SchedulerCompletedClient()),
        patch.object(messages_mod, "scan_and_tag", new=AsyncMock()) as mock_scan,
    ):
        terminal, _plan = await _reconcile_run_completion(
            scheduler_url="http://scheduler",
            tenant_id=TEST_TENANT,
            user_id=TEST_USER,
            run=run,
            svc=svc,
            knowledge_ingest_url="",  # skip memory write
            redis_url=None,  # take the summary-fallback persist path
            dlp_url="http://dlp",
        )

    assert terminal is True
    # Assistant summary row was persisted.
    svc.create_message.assert_awaited()
    # DLP scan fired exactly once on the persisted assistant text.
    mock_scan.assert_awaited_once()
    kwargs = mock_scan.await_args.kwargs
    assert kwargs["content"] == _SUMMARY
    assert kwargs["tenant_id"] == TEST_TENANT
    assert kwargs["dlp_url"] == "http://dlp"
    assert kwargs["resource_id"] == "msg_persisted1"
    assert kwargs["resource_type"] == "transcript"


@pytest.mark.asyncio
async def test_dlp_failure_does_not_break_finalization() -> None:
    """A DLP error is swallowed: the message still persists, run still terminal."""
    run = _completed_run()
    svc = _make_svc()

    async def _boom(*args: Any, **kw: Any) -> None:
        raise RuntimeError("dlp exploded")

    with (
        patch.object(httpx, "AsyncClient", return_value=_SchedulerCompletedClient()),
        patch.object(messages_mod, "scan_and_tag", new=AsyncMock(side_effect=_boom)) as mock_scan,
    ):
        terminal, _plan = await _reconcile_run_completion(
            scheduler_url="http://scheduler",
            tenant_id=TEST_TENANT,
            user_id=TEST_USER,
            run=run,
            svc=svc,
            knowledge_ingest_url="",
            redis_url=None,
            dlp_url="http://dlp",
        )

    # Fail-open: finalization completes despite the DLP blow-up.
    assert terminal is True
    svc.create_message.assert_awaited()
    mock_scan.assert_awaited_once()
    # Status was reconciled to COMPLETED on the run instance.
    assert run.status == "COMPLETED"


@pytest.mark.asyncio
async def test_no_dlp_url_skips_scan() -> None:
    """When dlp_url is unset (e.g. older caller), no scan is attempted."""
    run = _completed_run()
    svc = _make_svc()

    with (
        patch.object(httpx, "AsyncClient", return_value=_SchedulerCompletedClient()),
        patch.object(messages_mod, "scan_and_tag", new=AsyncMock()) as mock_scan,
    ):
        terminal, _plan = await _reconcile_run_completion(
            scheduler_url="http://scheduler",
            tenant_id=TEST_TENANT,
            user_id=TEST_USER,
            run=run,
            svc=svc,
            knowledge_ingest_url="",
            redis_url=None,
            # dlp_url omitted → defaults to "" → scan skipped.
        )

    assert terminal is True
    svc.create_message.assert_awaited()
    mock_scan.assert_not_awaited()
