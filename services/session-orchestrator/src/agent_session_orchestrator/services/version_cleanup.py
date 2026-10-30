"""Scheduled workspace file version cleanup.

Runs as an asyncio background task inside session-orchestrator (NOT the
user-facing cron-runner).  Reads admin-configurable retention settings from
platform_config and evicts expired/excess version records + their S3 blobs.

Scheduling
----------
Each tenant's cleanup runs on its own ``workspace.cleanup_cron`` (5-field cron,
evaluated in UTC; default ``0 3 * * *``).  The loop ticks once a minute,
reloads the per-tenant schedules every few minutes and catches up on minutes
missed while a previous cleanup was running.  The cross-tenant reads (which
tenants have versions, their cron) run on the migrator connection via
:func:`privileged_scalars` — a ``get_session`` bound to a pseudo tenant sees
nothing under FORCEd RLS.  Every delete runs in a tenant-bound transaction.

Blob deletion
-------------
Version rows are deleted first; their S3 objects are deleted only once no
attachment / workspace file / version row references them any more (a version
restore copies a version's ``blob_key`` into the current file, so the bytes
can still back live content).  See ``privacy_service.delete_unreferenced_blobs``.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from typing import Any

from sqlalchemy import delete, func, select

from agent_session_orchestrator.domain.db import get_session, privileged_scalars
from agent_session_orchestrator.domain.models import (
    PlatformConfig,
    WorkspaceFileVersion,
)
from agent_session_orchestrator.services.privacy_service import delete_unreferenced_blobs

logger = logging.getLogger(__name__)

_DEFAULT_RETENTION_DAYS = 90
_DEFAULT_MAX_VERSIONS = 20
_DEFAULT_CLEANUP_CRON = "0 3 * * *"
_CRON_KEY = "workspace.cleanup_cron"

# Same boot-stampede guard as the retention sweep: a fleet restarting together
# should not all hit the migrator connection at once.
_STARTUP_DELAY_SECONDS = 60
_TICK_SECONDS = 60
_SCHEDULE_REFRESH = dt.timedelta(minutes=10)
# Minutes of schedule evaluated after a long cleanup or a stalled loop; older
# slots are skipped rather than replayed.
_MAX_CATCHUP_MINUTES = 60
_ERROR_BACKOFF_SECONDS = 300


# ── cron ────────────────────────────────────────────────────────────────────


def _parse_field(spec: str, lo: int, hi: int) -> frozenset[int]:
    values: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            raise ValueError("empty cron list item")
        base, _, step_s = part.partition("/")
        step = int(step_s) if step_s else 1
        if step < 1:
            raise ValueError("cron step must be >= 1")
        if base == "*":
            start, end = lo, hi
        elif "-" in base:
            a, _, b = base.partition("-")
            start, end = int(a), int(b)
        else:
            start = int(base)
            end = hi if step_s else start
        if start < lo or end > hi or start > end:
            raise ValueError(f"cron value out of range: {part!r}")
        values.update(range(start, end + 1, step))
    return frozenset(values)


class CronSchedule:
    """A 5-field cron expression (``minute hour day-of-month month day-of-week``).

    Supports ``*``, numbers, ``a-b`` ranges, ``,`` lists and ``/step``.  Day of
    week is 0-7 with both 0 and 7 meaning Sunday.  When both day fields are
    restricted a time matches if *either* does (standard cron semantics).
    """

    def __init__(self, expr: str) -> None:
        fields = expr.split()
        if len(fields) != 5:
            raise ValueError(f"cron expression needs 5 fields: {expr!r}")
        self.expr = expr
        self._minute = _parse_field(fields[0], 0, 59)
        self._hour = _parse_field(fields[1], 0, 23)
        self._dom = _parse_field(fields[2], 1, 31)
        self._month = _parse_field(fields[3], 1, 12)
        dow = _parse_field(fields[4], 0, 7)
        self._dow = frozenset(d % 7 for d in dow)
        # Vixie-cron rule: a day field starting with "*" (incl. "*/2") does not
        # take part in the day-of-month / day-of-week OR.
        self._dom_any = fields[2].startswith("*")
        self._dow_any = fields[4].startswith("*")

    def matches(self, when: dt.datetime) -> bool:
        if when.minute not in self._minute or when.hour not in self._hour:
            return False
        if when.month not in self._month:
            return False
        dom_ok = when.day in self._dom
        dow_ok = (when.weekday() + 1) % 7 in self._dow  # cron: 0 = Sunday
        if self._dom_any or self._dow_any:
            return dom_ok and dow_ok
        return dom_ok or dow_ok


def parse_cron(expr: str | None) -> CronSchedule:
    """Parse ``expr``; an empty or invalid expression falls back to the default."""
    if expr and expr.strip():
        try:
            return CronSchedule(expr.strip())
        except ValueError:
            logger.warning("invalid %s %r; using %r", _CRON_KEY, expr, _DEFAULT_CLEANUP_CRON)
    return CronSchedule(_DEFAULT_CLEANUP_CRON)


# ── per-tenant cleanup ──────────────────────────────────────────────────────


async def _read_config(tenant_id: str) -> tuple[int, int]:
    retention_days = _DEFAULT_RETENTION_DAYS
    max_versions = _DEFAULT_MAX_VERSIONS
    try:
        async for db in get_session(tenant_id=tenant_id, user_id="svc-cleanup", actor="SYSTEM"):
            for key in ("workspace.version_retention_days", "workspace.max_versions_per_file"):
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
) -> dict[str, Any]:
    retention_days, max_versions = await _read_config(tenant_id)
    cutoff = dt.datetime.now(tz=dt.UTC) - dt.timedelta(days=retention_days)

    expired_count = 0
    excess_count = 0
    blobs_deleted = 0

    try:
        async for db in get_session(tenant_id=tenant_id, user_id="svc-cleanup", actor="SYSTEM"):
            victims: dict[str, str] = {}  # version id → blob_key

            expired_stmt = select(WorkspaceFileVersion.id, WorkspaceFileVersion.blob_key).where(
                WorkspaceFileVersion.tenant_id == tenant_id,
                WorkspaceFileVersion.created_at < cutoff,
            )
            for ver_id, key in (await db.execute(expired_stmt)).all():
                victims[ver_id] = key
            expired_count = len(victims)

            count_stmt = (
                select(
                    WorkspaceFileVersion.workspace_file_id,
                    func.count().label("cnt"),
                )
                .where(WorkspaceFileVersion.tenant_id == tenant_id)
                .group_by(WorkspaceFileVersion.workspace_file_id)
                .having(func.count() > max_versions)
            )
            for file_id, cnt in (await db.execute(count_stmt)).all():
                excess = cnt - max_versions
                oldest_stmt = (
                    select(WorkspaceFileVersion.id, WorkspaceFileVersion.blob_key)
                    .where(
                        WorkspaceFileVersion.workspace_file_id == file_id,
                        WorkspaceFileVersion.tenant_id == tenant_id,
                    )
                    .order_by(WorkspaceFileVersion.version.asc())
                    .limit(excess)
                )
                for ver_id, key in (await db.execute(oldest_stmt)).all():
                    if ver_id not in victims:
                        victims[ver_id] = key
                        excess_count += 1

            ids = list(victims)
            for start in range(0, len(ids), 500):
                await db.execute(
                    delete(WorkspaceFileVersion)
                    .where(
                        WorkspaceFileVersion.tenant_id == tenant_id,
                        WorkspaceFileVersion.id.in_(ids[start:start + 500]),
                    )
                    .execution_options(synchronize_session=False)
                )
            await db.flush()
            blobs_deleted = await delete_unreferenced_blobs(
                db, tenant_id=tenant_id, keys=victims.values(), blob_store=blob_store,
            )
    except Exception:
        logger.exception("version cleanup failed for tenant %s", tenant_id)

    return {
        "tenant_id": tenant_id,
        "expired_deleted": expired_count,
        "excess_deleted": excess_count,
        "blobs_deleted": blobs_deleted,
    }


# ── loop ────────────────────────────────────────────────────────────────────


async def load_tenant_schedules(settings: Any) -> dict[str, CronSchedule]:
    """Cron schedule of every tenant that has file versions (cross-tenant read)."""
    rows = await privileged_scalars(
        settings,
        "SELECT v.tenant_id || '\x1f' || COALESCE(c.value #>> '{}', '')"
        " FROM (SELECT DISTINCT tenant_id"
        "       FROM session_orchestrator.workspace_file_version) v"
        " LEFT JOIN session_orchestrator.platform_config c"
        "   ON c.tenant_id = v.tenant_id AND c.key = :key",
        {"key": _CRON_KEY},
    )
    schedules: dict[str, CronSchedule] = {}
    for row in rows:
        tenant_id, _, expr = str(row).partition("\x1f")
        if tenant_id:
            schedules[tenant_id] = parse_cron(expr)
    return schedules


def due_tenants(
    schedules: dict[str, CronSchedule], after: dt.datetime, until: dt.datetime,
) -> list[str]:
    """Tenants whose schedule fires in a minute in ``(after, until]`` (minute floors)."""
    start = max(after, until - dt.timedelta(minutes=_MAX_CATCHUP_MINUTES))
    due: set[str] = set()
    minute = start + dt.timedelta(minutes=1)
    while minute <= until:
        due.update(tid for tid, sched in schedules.items() if sched.matches(minute))
        minute += dt.timedelta(minutes=1)
    return sorted(due)


def _utcnow() -> dt.datetime:
    return dt.datetime.now(tz=dt.UTC)


def _minute_floor(when: dt.datetime) -> dt.datetime:
    return when.replace(second=0, microsecond=0)


async def run_version_cleanup_loop(settings: Any, *, blob_store: Any | None = None) -> None:
    """Run each tenant's cleanup on its ``workspace.cleanup_cron``.  Started in lifespan.

    ``blob_store`` is the app's S3 store; without one, version rows are still
    evicted but their objects are left in place (logged).
    """
    await asyncio.sleep(_STARTUP_DELAY_SECONDS)
    last_checked = _minute_floor(_utcnow())
    schedules: dict[str, CronSchedule] = {}
    loaded_at: dt.datetime | None = None
    while True:
        try:
            now = _minute_floor(_utcnow())
            if loaded_at is None or now - loaded_at >= _SCHEDULE_REFRESH:
                schedules = await load_tenant_schedules(settings)
                loaded_at = now
            due = due_tenants(schedules, last_checked, now)
            last_checked = now
            if due:
                logger.info("version cleanup: starting for %d tenant(s)", len(due))
                stats = [await cleanup_tenant_versions(tid, blob_store) for tid in due]
                logger.info(
                    "version cleanup complete: %d tenants, %d expired, %d excess, %d blobs",
                    len(stats),
                    sum(s["expired_deleted"] for s in stats),
                    sum(s["excess_deleted"] for s in stats),
                    sum(s["blobs_deleted"] for s in stats),
                )
            # Wake just after the next minute boundary.
            current = _utcnow()
            await asyncio.sleep(_TICK_SECONDS - current.second - current.microsecond / 1e6 + 1)
        except asyncio.CancelledError:
            logger.info("version cleanup loop cancelled")
            break
        except Exception:
            logger.exception("version cleanup loop error, retrying in %ds", _ERROR_BACKOFF_SECONDS)
            await asyncio.sleep(_ERROR_BACKOFF_SECONDS)
