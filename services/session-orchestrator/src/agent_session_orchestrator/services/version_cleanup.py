"""Daily workspace file version cleanup.

Runs as an asyncio background task inside session-orchestrator (NOT the
user-facing cron-runner).  Reads admin-configurable retention settings from
platform_config and evicts expired/excess version records + their S3 blobs.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_session_orchestrator.domain.db import get_session
from agent_session_orchestrator.domain.models import (
    PlatformConfig,
    WorkspaceFileVersion,
)

logger = logging.getLogger(__name__)

_DEFAULT_RETENTION_DAYS = 90
_DEFAULT_MAX_VERSIONS = 20
_DEFAULT_CLEANUP_HOUR = 3


async def _read_config(tenant_id: str) -> tuple[int, int]:
    retention_days = _DEFAULT_RETENTION_DAYS
    max_versions = _DEFAULT_MAX_VERSIONS
    try:
        async for db in get_session(tenant_id=tenant_id, user_id="svc-cleanup", actor="SYSTEM"):
            for key, default in [
                ("workspace.version_retention_days", _DEFAULT_RETENTION_DAYS),
                ("workspace.max_versions_per_file", _DEFAULT_MAX_VERSIONS),
            ]:
                result = await db.execute(
                    select(PlatformConfig.value).where(
                        PlatformConfig.tenant_id == tenant_id,
                        PlatformConfig.key == key,
                    )
                )
                val = result.scalar_one_or_none()
                if val is not None:
                    parsed = int(val)
                    if key.endswith("retention_days"):
                        retention_days = parsed
                    else:
                        max_versions = parsed
    except Exception:
        logger.warning("failed to read cleanup config for tenant %s, using defaults", tenant_id)
    return retention_days, max_versions


async def cleanup_tenant_versions(
    tenant_id: str,
    blob_store: Any | None,
) -> dict[str, int]:
    retention_days, max_versions = await _read_config(tenant_id)
    cutoff = dt.datetime.now(tz=dt.UTC) - dt.timedelta(days=retention_days)

    expired_count = 0
    excess_count = 0
    blob_errors = 0

    try:
        async for db in get_session(tenant_id=tenant_id, user_id="svc-cleanup", actor="SYSTEM"):
            expired_stmt = (
                select(WorkspaceFileVersion)
                .where(
                    WorkspaceFileVersion.tenant_id == tenant_id,
                    WorkspaceFileVersion.created_at < cutoff,
                )
            )
            for ver in (await db.execute(expired_stmt)).scalars().all():
                if blob_store and ver.blob_key:
                    try:
                        blob_store.delete(ver.blob_key)
                    except Exception:
                        blob_errors += 1
                await db.delete(ver)
                expired_count += 1

            count_stmt = (
                select(
                    WorkspaceFileVersion.workspace_file_id,
                    func.count().label("cnt"),
                )
                .where(WorkspaceFileVersion.tenant_id == tenant_id)
                .group_by(WorkspaceFileVersion.workspace_file_id)
                .having(func.count() > max_versions)
            )
            for file_id, cnt in await db.execute(count_stmt):
                excess = cnt - max_versions
                oldest_stmt = (
                    select(WorkspaceFileVersion)
                    .where(
                        WorkspaceFileVersion.workspace_file_id == file_id,
                        WorkspaceFileVersion.tenant_id == tenant_id,
                    )
                    .order_by(WorkspaceFileVersion.version.asc())
                    .limit(excess)
                )
                for ver in (await db.execute(oldest_stmt)).scalars().all():
                    if blob_store and ver.blob_key:
                        try:
                            blob_store.delete(ver.blob_key)
                        except Exception:
                            blob_errors += 1
                    await db.delete(ver)
                    excess_count += 1

            await db.flush()
    except Exception:
        logger.exception("version cleanup failed for tenant %s", tenant_id)

    return {
        "tenant_id": tenant_id,
        "expired_deleted": expired_count,
        "excess_deleted": excess_count,
        "blob_errors": blob_errors,
    }


async def run_version_cleanup_loop(settings: Any) -> None:
    """Sleep until cleanup hour, run cleanup, repeat.  Started as a task in lifespan."""
    while True:
        try:
            now = dt.datetime.now(tz=dt.UTC)
            target = now.replace(hour=_DEFAULT_CLEANUP_HOUR, minute=0, second=0, microsecond=0)
            if target <= now:
                target += dt.timedelta(days=1)
            sleep_seconds = (target - now).total_seconds()

            logger.info(
                "version cleanup: next run in %.0f seconds (at %s)",
                sleep_seconds, target.isoformat(),
            )
            await asyncio.sleep(sleep_seconds)
            logger.info("version cleanup: starting")

            blob_store = getattr(settings, "blob_store", None)

            tenant_ids: list[str] = []
            async for db in get_session(tenant_id="SYSTEM", user_id="svc-cleanup", actor="SYSTEM"):
                result = await db.execute(
                    select(WorkspaceFileVersion.tenant_id).distinct()
                )
                tenant_ids = [row[0] for row in result.all()]

            stats = []
            for tid in tenant_ids:
                s = await cleanup_tenant_versions(tid, blob_store)
                stats.append(s)

            total_expired = sum(s["expired_deleted"] for s in stats)
            total_excess = sum(s["excess_deleted"] for s in stats)
            logger.info(
                "version cleanup complete: %d tenants, %d expired, %d excess deleted",
                len(stats), total_expired, total_excess,
            )
        except asyncio.CancelledError:
            logger.info("version cleanup loop cancelled")
            break
        except Exception:
            logger.exception("version cleanup loop error, retrying in 1h")
            await asyncio.sleep(3600)
