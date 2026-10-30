"""Workspace version cleanup — cron schedule, cross-tenant scan, blob hygiene, loop tick."""

from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from agent_session_orchestrator.domain.models import (
    PlatformConfig,
    Workspace,
    WorkspaceFile,
    WorkspaceFileVersion,
)
from agent_session_orchestrator.services import version_cleanup as vc
from tests._helpers import TEST_TENANT, TEST_USER
from tests._privacy_harness import FakeBlobStore, new_db, ts


def _run(coro: Any) -> Any:
    return asyncio.get_event_loop().run_until_complete(coro)


def _at(text: str) -> dt.datetime:
    return dt.datetime.fromisoformat(text).replace(tzinfo=dt.UTC)


# ── cron ─────────────────────────────────────────────────────────────────────


def test_cron_daily_default() -> None:
    sched = vc.parse_cron("0 3 * * *")
    assert sched.matches(_at("2026-10-30T03:00"))
    assert not sched.matches(_at("2026-10-30T03:01"))
    assert not sched.matches(_at("2026-10-30T04:00"))


def test_cron_weekly_from_admin_ui() -> None:
    # The admin editor writes "M H * * d,d" with 0 = Sunday.
    sched = vc.parse_cron("30 2 * * 1,3")
    assert sched.matches(_at("2026-11-02T02:30"))  # Monday
    assert sched.matches(_at("2026-11-04T02:30"))  # Wednesday
    assert not sched.matches(_at("2026-11-03T02:30"))  # Tuesday


def test_cron_sunday_is_0_and_7() -> None:
    sunday = _at("2026-11-01T05:00")
    assert vc.parse_cron("0 5 * * 0").matches(sunday)
    assert vc.parse_cron("0 5 * * 7").matches(sunday)


def test_cron_monthly_and_steps_and_ranges() -> None:
    assert vc.parse_cron("0 4 15 * *").matches(_at("2026-10-15T04:00"))
    assert not vc.parse_cron("0 4 15 * *").matches(_at("2026-10-16T04:00"))
    every_15 = vc.parse_cron("*/15 * * * *")
    assert [m for m in range(60) if every_15.matches(_at(f"2026-10-30T10:{m:02d}"))] == [
        0, 15, 30, 45,
    ]
    business = vc.parse_cron("0 9-17/4 * * 1-5")
    hours = [h for h in range(24) if business.matches(_at(f"2026-10-30T{h:02d}:00"))]
    assert hours == [9, 13, 17]  # Friday


def test_cron_day_of_month_or_day_of_week() -> None:
    # Both restricted → either matches (standard cron).
    sched = vc.parse_cron("0 0 1 * 1")
    assert sched.matches(_at("2026-10-01T00:00"))  # 1st (Thursday)
    assert sched.matches(_at("2026-10-05T00:00"))  # Monday
    assert not sched.matches(_at("2026-10-06T00:00"))


@pytest.mark.parametrize(
    "expr", ["", "   ", None, "61 3 * * *", "0 3 * *", "a b c d e", "*/0 * * * *"],
)
def test_invalid_cron_falls_back_to_default(expr: str | None) -> None:
    sched = vc.parse_cron(expr)
    assert sched.expr == "0 3 * * *"


def test_due_tenants_window_and_catch_up_cap() -> None:
    schedules = {
        "tnt_a": vc.parse_cron("0 3 * * *"),
        "tnt_b": vc.parse_cron("5 3 * * *"),
        "tnt_c": vc.parse_cron("0 1 * * *"),
    }
    # (02:59, 03:00] → only tnt_a; the left edge is exclusive.
    assert vc.due_tenants(schedules, _at("2026-10-30T02:59"), _at("2026-10-30T03:00")) == ["tnt_a"]
    assert vc.due_tenants(schedules, _at("2026-10-30T03:00"), _at("2026-10-30T03:00")) == []
    # A long cleanup run: both 03:00 and 03:05 fall inside the window.
    assert vc.due_tenants(
        schedules, _at("2026-10-30T02:58"), _at("2026-10-30T03:10"),
    ) == ["tnt_a", "tnt_b"]
    # A stalled loop only replays the last hour, not 01:00.
    assert vc.due_tenants(
        schedules, _at("2026-10-29T23:00"), _at("2026-10-30T03:30"),
    ) == ["tnt_a", "tnt_b"]


# ── cross-tenant schedule scan ───────────────────────────────────────────────


def test_load_tenant_schedules_uses_privileged_scan() -> None:
    seen: list[tuple[str, dict[str, Any]]] = []

    async def _privileged(settings: Any, sql: str, params: dict[str, Any]) -> list[str]:
        seen.append((sql, params))
        return ["tnt_a\x1f15 4 * * *", "tnt_b\x1f", "tnt_c\x1fnot a cron", "\x1fjunk"]

    with patch.object(vc, "privileged_scalars", _privileged):
        schedules = _run(vc.load_tenant_schedules(object()))
    assert set(schedules) == {"tnt_a", "tnt_b", "tnt_c"}
    assert schedules["tnt_a"].expr == "15 4 * * *"
    assert schedules["tnt_b"].expr == "0 3 * * *"
    assert schedules["tnt_c"].expr == "0 3 * * *"
    ((sql, params),) = seen
    assert "FROM session_orchestrator.workspace_file_version" in sql
    assert "LEFT JOIN session_orchestrator.platform_config" in sql
    assert params == {"key": "workspace.cleanup_cron"}


# ── per-tenant cleanup ───────────────────────────────────────────────────────


def _seed_versions(sync: Any) -> None:
    t = TEST_TENANT
    sync.add(Workspace(id="wsp_1", tenant_id=t, user_id=TEST_USER, name="w", skills=[], meta={}))
    sync.flush()
    sync.add_all([
        # A restore copied v2's key into the current file.
        WorkspaceFile(id="wsf_1", tenant_id=t, workspace_id="wsp_1", user_id=TEST_USER,
                      filename="a.md", blob_key="k/v2", current_version=5),
        PlatformConfig(tenant_id=t, key="workspace.version_retention_days", value=30),
        PlatformConfig(tenant_id=t, key="workspace.max_versions_per_file", value=2),
    ])
    sync.flush()
    sync.add_all([
        WorkspaceFileVersion(id="wfv_1", tenant_id=t, workspace_file_id="wsf_1", version=1,
                             blob_key="k/v1", size_bytes=1, created_at=ts(60)),
        WorkspaceFileVersion(id="wfv_2", tenant_id=t, workspace_file_id="wsf_1", version=2,
                             blob_key="k/v2", size_bytes=1, created_at=ts(45)),
        WorkspaceFileVersion(id="wfv_3", tenant_id=t, workspace_file_id="wsf_1", version=3,
                             blob_key="k/v3", size_bytes=1, created_at=ts(5)),
        WorkspaceFileVersion(id="wfv_4", tenant_id=t, workspace_file_id="wsf_1", version=4,
                             blob_key="k/v4", size_bytes=1, created_at=ts(1)),
    ])
    sync.commit()


def test_cleanup_evicts_expired_and_excess_and_keeps_shared_blobs() -> None:
    db, sync = new_db()
    _seed_versions(sync)

    async def _gen(**kw: Any) -> AsyncIterator[Any]:
        yield db

    blobs = FakeBlobStore()
    with patch.object(vc, "get_session", _gen):
        stats = _run(vc.cleanup_tenant_versions(TEST_TENANT, blobs))
    sync.commit()

    # v1 + v2 expired (> 30 days); 2 left = the cap, so nothing in excess.
    assert stats == {
        "tenant_id": TEST_TENANT, "expired_deleted": 2, "excess_deleted": 0,
        "blobs_deleted": 1,
    }
    remaining = sorted(sync.execute(select(WorkspaceFileVersion.id)).scalars().all())
    assert remaining == ["wfv_3", "wfv_4"]
    # k/v2 still backs the current file → kept; k/v1 is gone.
    assert blobs.deleted == ["k/v1"]


def test_cleanup_enforces_max_versions() -> None:
    db, sync = new_db()
    _seed_versions(sync)
    cfg = sync.execute(
        select(PlatformConfig).where(PlatformConfig.key == "workspace.version_retention_days")
    ).scalar_one()
    cfg.value = 365
    sync.commit()

    async def _gen(**kw: Any) -> AsyncIterator[Any]:
        yield db

    blobs = FakeBlobStore()
    with patch.object(vc, "get_session", _gen):
        stats = _run(vc.cleanup_tenant_versions(TEST_TENANT, blobs))
    sync.commit()
    assert stats["expired_deleted"] == 0 and stats["excess_deleted"] == 2
    remaining = sorted(sync.execute(select(WorkspaceFileVersion.id)).scalars().all())
    assert remaining == ["wfv_3", "wfv_4"]
    assert blobs.deleted == ["k/v1"]


def test_cleanup_without_blob_store_still_evicts_rows() -> None:
    db, sync = new_db()
    _seed_versions(sync)

    async def _gen(**kw: Any) -> AsyncIterator[Any]:
        yield db

    with patch.object(vc, "get_session", _gen), \
            patch("agent_session_orchestrator.services.privacy_service.registered_blob_store",
                  lambda: None):
        stats = _run(vc.cleanup_tenant_versions(TEST_TENANT, None))
    assert stats["expired_deleted"] == 2 and stats["blobs_deleted"] == 0


# ── loop tick ────────────────────────────────────────────────────────────────


def test_loop_runs_due_tenant_with_app_blob_store() -> None:
    clock = iter([
        _at("2026-10-30T03:00:10"),  # last_checked after the startup delay
        _at("2026-10-30T03:00:20"),  # tick 1: window (03:00, 03:00] → nothing
        _at("2026-10-30T03:00:20"),  # sleep computation
        _at("2026-10-30T03:01:05"),  # tick 2: window (03:00, 03:01] → due
        _at("2026-10-30T03:01:05"),
    ])
    sleeps = AsyncMock(side_effect=[None, None, asyncio.CancelledError()])
    cleanup = AsyncMock(return_value={
        "tenant_id": "tnt_a", "expired_deleted": 1, "excess_deleted": 0, "blobs_deleted": 1,
    })
    schedules = AsyncMock(return_value={
        "tnt_a": vc.parse_cron("1 3 * * *"), "tnt_b": vc.parse_cron("0 4 * * *"),
    })
    store = FakeBlobStore()
    with patch.object(vc, "_utcnow", lambda: next(clock)), \
            patch.object(vc.asyncio, "sleep", sleeps), \
            patch.object(vc, "load_tenant_schedules", schedules), \
            patch.object(vc, "cleanup_tenant_versions", cleanup):
        _run(vc.run_version_cleanup_loop(object(), blob_store=store))
    cleanup.assert_awaited_once_with("tnt_a", store)
    schedules.assert_awaited_once()  # refreshed at most every few minutes
    assert sleeps.call_args_list[0].args == (vc._STARTUP_DELAY_SECONDS,)
