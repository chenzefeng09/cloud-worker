"""Background reconcilers — persist terminal state for HEADLESS runs.

Problem
-------
``_reconcile_run_completion`` in ``api.v1.messages`` is called only when a
client actively polls the session (SSE stream or GET /messages).  Runs nobody
is watching — a task on the board, a run the cron-runner triggered
(``source='schedule'``) — have no browser viewer: the sandbox exits,
``result.json`` is written, but the ``Run`` row stays ``RUNNING`` /
``PENDING``.

Solution
--------
Two background asyncio loops call the same ``_reconcile_run_completion``
helper that the normal poll path calls:

* **Completion watcher** (``run_completion_watcher``), every
  ``COMPLETION_POLL_SECONDS``: reads the newest entry of every live run's
  token stream (one pipelined Redis round trip) and reconciles the runs whose
  worker has written its final ``end`` marker.  A finished run is settled
  within seconds, and the scheduler is polled only for runs that ended.  An
  ended run the scheduler still reports live (its container is shutting down)
  is probed again after a growing delay.
* **Stale-run sweeper** (``run_sweeper``), every ``SWEEP_INTERVAL_SECONDS``:
  the safety net for everything the watcher cannot see — a worker that died
  without an end marker, a vanished sandbox, an expired or lost stream, no
  Redis at all.  It probes every run older than ``STALE_AFTER_SECONDS``.

Several reconcilers may race on one run (both loops, an open SSE stream, more
than one orchestrator replica); ``claim_run_transition`` lets exactly one of
them finalize it.

Cross-tenant read
-----------------
RLS is ``FORCE``-enabled on ``app_user``, so a plain ``get_session`` would
return zero rows for any tenant other than the one bound to the current
transaction.  Following the exact pattern established by
``services.session_service.purge_expired_sessions``, the stale-run scan (and
the completion watcher's scan of live sandbox runs) uses
``domain.db.privileged_scalars`` — a reused, single-connection engine on the
``app_migrator`` (``BYPASSRLS``) DSN — and reads each row's tenant, run,
session and session user (``_ScannedRun``).  Each *write* (reconcile) then
goes through the normal ``app_user`` ``get_session`` path with the correct
``tenant_id`` so RLS still guards every mutation.

Failure-open
------------
* A crash inside a single-run reconcile is logged and the loop continues.
* The loop itself never dies unless cancelled (shutdown).
* ``asyncio.CancelledError`` is re-raised so the lifespan ``finally`` block
  can ``await sweep_task`` cleanly.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI

from agent_session_orchestrator.domain.db import get_session as acquire_db_session
from agent_session_orchestrator.domain.db import privileged_scalars
from agent_session_orchestrator.services.session_service import SessionService

logger = logging.getLogger(__name__)

# How often the sweeper wakes up to look for stale runs.
SWEEP_INTERVAL_SECONDS: int = 60

# A run must be at least this old (from started_at, or created_at when
# started_at is NULL) before we probe the scheduler.  This avoids hammering
# freshly-created runs that are still cold-starting their sandbox.
STALE_AFTER_SECONDS: int = 120

# Legacy inline (chat-mode) runs — a mode that has since been removed — have no
# sandbox and no scheduler container, so the scheduler reconciler can never
# resolve them; any such row left RUNNING would otherwise stay RUNNING forever.
# After this window a legacy inline run still stuck RUNNING/PENDING is
# force-FAILED.
INLINE_ORPHAN_AFTER_SECONDS: int = 20 * 60

# Short startup delay so a fleet of pods restarting together does not all hit
# the DB at the same instant.
_SWEEP_STARTUP_DELAY_SECONDS: int = 30

# How often the completion watcher looks for runs whose worker has finished.
COMPLETION_POLL_SECONDS: float = 2.0

# An ended run the scheduler still reports live is probed again after a delay
# that doubles from COMPLETION_POLL_SECONDS up to this cap.
_ENDED_PROBE_MAX_DELAY_SECONDS: float = 30.0

_WATCH_STARTUP_DELAY_SECONDS: int = 5

# Cross-tenant scans (BYPASSRLS migrator connection).  Four values are encoded
# as one string to fit the scalar helper's one-column contract:
# "tenant_id\x1frun_id\x1fsession_id\x1fuser_id".
#
# The session.user_id is carried out of the scan directly via a LEFT JOIN on
# session_orchestrator.session.  A LEFT JOIN (not INNER) is deliberate: a
# session soft-deleted by the 30-day retention sweep only has its
# ``deleted_at`` column set — the row still exists — so the JOIN resolves its
# user_id even though the tenant-bound ``get_session`` path (which filters
# ``deleted_at IS NULL``) would raise SessionNotFoundError for it.  Without
# this, orphaned runs of soft-deleted sessions fell back to a ``SYSTEM``
# user_id, which the scheduler rejects with 403 → run stuck RUNNING forever.
# COALESCE guards the (rare) case of a run whose session row is genuinely
# gone, yielding an empty string handled in ``_parse_scan_row``.
_SCAN_COLUMNS = (
    "SELECT r.tenant_id || '\x1f' || r.id || '\x1f' || r.session_id"
    "       || '\x1f' || COALESCE(s.user_id, '')"
    "  FROM session_orchestrator.run r"
    "  LEFT JOIN session_orchestrator.session s ON s.id = r.session_id"
    " WHERE r.status IN ('RUNNING', 'PENDING')"
)
_STALE_RUNS_SQL = (
    _SCAN_COLUMNS
    + "   AND COALESCE(r.started_at, r.created_at) < :cutoff"
    " ORDER BY r.tenant_id"
)
# Only sandbox runs can end with a marker the watcher reads.
_LIVE_SANDBOX_RUNS_SQL = _SCAN_COLUMNS + "   AND r.sandbox_id IS NOT NULL ORDER BY r.tenant_id"


@dataclass(frozen=True)
class _ScannedRun:
    tenant_id: str
    run_id: str
    session_id: str
    # Empty when the run's session row is genuinely gone.
    user_id: str


def _parse_scan_row(row: object) -> _ScannedRun | None:
    parts = str(row).split("\x1f", 3)
    if len(parts) != 4:
        logger.warning("reconcile: malformed scan row %r — skipping", row)
        return None
    tenant_id, run_id, session_id, user_id = parts
    # user_id may legitimately be an empty string (session row genuinely
    # gone); only the first three fields are required to identify a run.
    if not tenant_id or not run_id or not session_id:
        logger.warning("reconcile: empty field in scan row %r — skipping", row)
        return None
    return _ScannedRun(tenant_id, run_id, session_id, user_id)


def _inline_run_is_orphaned(run_obj: object) -> bool:
    """True if an inline run is old enough to be considered process-orphaned.

    Age is measured from ``started_at`` (or ``created_at`` when started_at is
    NULL).  Both are tz-aware DateTime(timezone=True); guard a naive value
    defensively so the comparison never raises.
    """
    reference = getattr(run_obj, "started_at", None) or getattr(
        run_obj, "created_at", None
    )
    if reference is None:
        return False
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=dt.UTC)
    age = (dt.datetime.now(tz=dt.UTC) - reference).total_seconds()
    return age >= INLINE_ORPHAN_AFTER_SECONDS


async def _fail_orphaned_inline_run(
    *, run_obj: object, redis_url: str | None,
) -> None:
    """Mark an orphaned inline run FAILED and push the ``end`` SSE marker.

    Mutates the ORM instance in place; the surrounding tenant-bound transaction
    commits it on teardown (same discipline as ``_reconcile_run_completion``).
    Fail-open on the Redis push: the DB status flip is what matters; a missing
    end marker only affects an (unlikely) still-open SSE tail.
    """
    run_obj.status = "FAILED"  # type: ignore[attr-defined]
    run_obj.finished_at = dt.datetime.now(tz=dt.UTC)  # type: ignore[attr-defined]
    run_obj.error_message = (  # type: ignore[attr-defined]
        "inline run orphaned (process restart?)"
    )
    run_obj.result_summary = (  # type: ignore[attr-defined]
        "inline run orphaned (process restart?)"
    )

    if redis_url:
        try:
            import redis.asyncio as aioredis  # noqa: PLC0415

            from agent_session_orchestrator.api.v1.messages import (  # noqa: PLC0415
                _run_token_stream_key,
            )

            client = aioredis.Redis.from_url(
                redis_url,
                socket_connect_timeout=2,
                socket_timeout=2,
                decode_responses=True,
            )
            try:
                await client.xadd(
                    _run_token_stream_key(run_obj.id),  # type: ignore[attr-defined]
                    {"type": "end"},
                )
            finally:
                with contextlib.suppress(Exception):
                    await client.aclose()
        except Exception:  # noqa: BLE001 — end marker is best-effort
            logger.debug(
                "reconcile_sweeper: end marker push failed for orphaned inline run",
                extra={"run_id": getattr(run_obj, "id", "?")},
            )


async def _reconcile_scanned_run(settings: Any, scanned: _ScannedRun) -> str | None:
    """Reconcile one scanned run in its own tenant-bound transaction.

    Returns the run's status afterwards, or ``None`` when it could not be
    loaded or reconciling it failed (logged; the caller's loop carries on).
    """
    # Lazy import to avoid circular imports at module load time.
    from agent_session_orchestrator.api.v1.messages import (
        _reconcile_run_completion,  # noqa: PLC0415
    )

    tenant_id, run_id, session_id = scanned.tenant_id, scanned.run_id, scanned.session_id
    redis_url: str | None = settings.redis_url
    try:
        async for db in acquire_db_session(
            tenant_id=tenant_id, user_id="SYSTEM", actor="SYSTEM"
        ):
            svc = SessionService(db, tenant_id=tenant_id)

            # user_id is taken from the cross-tenant scan (LEFT JOIN on
            # session), which — unlike the RLS-bound get_session below —
            # resolves it even for soft-deleted sessions.  This keeps the
            # scheduler authz happy (a real user, never SYSTEM) for
            # orphaned runs of retention-purged sessions.
            user_id = scanned.user_id
            # get_session is still probed purely for observability: for a
            # soft-deleted session it raises, which is the expected signal
            # that this run is orphaned.  It never overrides user_id.
            if not user_id:
                # Scan produced no user_id (session row genuinely gone).
                # Fall back: get_session if it somehow succeeds, else the
                # historical SYSTEM sentinel.
                try:
                    session_obj = await svc.get_session(session_id)
                    user_id = session_obj.user_id
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "reconcile_sweeper: no user_id from scan and "
                        "session not found, falling back to SYSTEM",
                        extra={"tenant_id": tenant_id, "session_id": session_id},
                    )
                    user_id = "SYSTEM"
            else:
                try:
                    await svc.get_session(session_id)
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "reconcile_sweeper: session not visible via RLS "
                        "(likely soft-deleted); reconciling orphaned run "
                        "with its real user_id from scan",
                        extra={"tenant_id": tenant_id, "session_id": session_id},
                    )

            # Re-fetch the run inside the RLS-scoped session.
            runs = await svc.list_runs(session_id)
            run_obj = next((r for r in runs if r.id == run_id), None)
            if run_obj is None:
                logger.warning(
                    "reconcile_sweeper: run not found in RLS session",
                    extra={"tenant_id": tenant_id, "run_id": run_id},
                )
                return None

            # ── Inline (chat-mode) orphan branch ───────────────────────────
            # An inline run (execution_mode='inline', sandbox_id IS NULL) is
            # never resolvable via the scheduler.  If it is still stuck past
            # the (much larger) inline window, its in-process executor task
            # died with a restarted process — force it FAILED and push the
            # ``end`` marker so any live SSE tail closes.  Deliberately does
            # NOT touch sandbox runs (which take the reconcile path below).
            if run_obj.execution_mode == "inline" and run_obj.sandbox_id is None:
                if _inline_run_is_orphaned(run_obj):
                    await _fail_orphaned_inline_run(run_obj=run_obj, redis_url=redis_url)
                    logger.info(
                        "reconcile_sweeper: orphaned inline run marked FAILED",
                        extra={"run_id": run_id, "tenant_id": tenant_id},
                    )
                else:
                    logger.debug(
                        "reconcile_sweeper: inline run within grace window; "
                        "leaving to its executor",
                        extra={"run_id": run_id},
                    )
                return str(run_obj.status)

            terminal, _ = await _reconcile_run_completion(
                scheduler_url=settings.agent_scheduler_url,
                tenant_id=tenant_id,
                user_id=user_id,
                run=run_obj,
                svc=svc,
                knowledge_ingest_url=settings.knowledge_ingest_url,
                redis_url=redis_url,
                dlp_url=settings.foundation_dlp_url,
            )
            if terminal:
                logger.info(
                    "reconcile_sweeper: run reconciled to terminal state %s",
                    run_obj.status,
                    extra={"run_id": run_id, "tenant_id": tenant_id},
                )
            return str(run_obj.status)
    except asyncio.CancelledError:
        raise  # propagate shutdown signal
    except Exception:  # noqa: BLE001
        logger.exception(
            "reconcile_sweeper: error reconciling run",
            extra={"tenant_id": tenant_id, "run_id": run_id},
        )
    return None


async def _sweep_once(app: FastAPI) -> None:
    """One sweep iteration: find stale runs and reconcile each one."""
    settings = app.state.settings
    cutoff = dt.datetime.now(tz=dt.UTC) - dt.timedelta(seconds=STALE_AFTER_SECONDS)
    rows = await privileged_scalars(settings, _STALE_RUNS_SQL, {"cutoff": cutoff})
    if not rows:
        logger.debug("reconcile_sweeper: no stale runs found")
        return

    logger.info("reconcile_sweeper: found %d stale run(s) to probe", len(rows))
    for row in rows:
        scanned = _parse_scan_row(row)
        if scanned is not None:
            await _reconcile_scanned_run(settings, scanned)


async def run_sweeper(app: FastAPI) -> None:
    """Async loop started from the FastAPI lifespan.

    Sleeps a short startup delay first (avoid boot-time DB stampede), then
    loops forever on ``SWEEP_INTERVAL_SECONDS``.  Any per-sweep error is
    logged and the loop keeps running.  ``asyncio.CancelledError`` propagates
    so the lifespan ``finally`` block can await the task cleanly.
    """
    await asyncio.sleep(_SWEEP_STARTUP_DELAY_SECONDS)
    logger.info(
        "reconcile_sweeper: starting (interval=%ds, stale_after=%ds)",
        SWEEP_INTERVAL_SECONDS,
        STALE_AFTER_SECONDS,
    )
    while True:
        try:
            await _sweep_once(app)
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover — keep the loop alive
            logger.exception("reconcile_sweeper: sweep tick failed")
        await asyncio.sleep(SWEEP_INTERVAL_SECONDS)


# ── Completion watcher ──────────────────────────────────────────────────────


async def _ended_runs(redis_url: str, run_ids: Sequence[str]) -> set[str]:
    """The runs whose token stream ends with the worker's ``end`` marker.

    One pipelined round trip reading the newest entry of each stream.  A leg
    paused for input also ends with a marker; reconciling such a run settles
    it as PAUSED.  A resumed leg appends ``pending_resolved`` before it runs,
    so it no longer reads as ended.
    """
    import redis.asyncio as aioredis  # noqa: PLC0415

    from agent_session_orchestrator.api.v1.messages import (  # noqa: PLC0415
        _coerce_redis_entry,
        _run_token_stream_key,
    )

    client = aioredis.Redis.from_url(
        redis_url, socket_connect_timeout=2, socket_timeout=2, decode_responses=True,
    )
    try:
        async with client.pipeline(transaction=False) as pipe:
            for run_id in run_ids:
                pipe.xrevrange(_run_token_stream_key(run_id), count=1)
            newest = await pipe.execute()
    finally:
        with contextlib.suppress(Exception):
            await client.aclose()
    return {
        run_id
        for run_id, entries in zip(run_ids, newest, strict=True)
        if entries and _coerce_redis_entry(entries[0][1]).get("type") == "end"
    }


async def _watch_once(app: FastAPI, retry_at: dict[str, tuple[float, float]]) -> None:
    """One watcher tick: reconcile the live runs whose worker has finished.

    ``retry_at`` maps a run that ended but is still live (the scheduler still
    reports its container running) to ``(not_before, delay)`` on the monotonic
    clock; it is kept across ticks and pruned of runs that are no longer live.
    """
    settings = app.state.settings
    rows = await privileged_scalars(settings, _LIVE_SANDBOX_RUNS_SQL)
    live = [scanned for scanned in map(_parse_scan_row, rows) if scanned is not None]
    live_ids = {scanned.run_id for scanned in live}
    for run_id in [r for r in retry_at if r not in live_ids]:
        del retry_at[run_id]

    now = time.monotonic()
    due = [scanned for scanned in live if retry_at.get(scanned.run_id, (0.0, 0.0))[0] <= now]
    if not due:
        return
    ended = await _ended_runs(settings.redis_url, [scanned.run_id for scanned in due])
    for scanned in due:
        if scanned.run_id not in ended:
            continue
        status = await _reconcile_scanned_run(settings, scanned)
        if status in ("RUNNING", "PENDING") or status is None:
            previous = retry_at.get(scanned.run_id, (0.0, COMPLETION_POLL_SECONDS / 2))[1]
            delay = min(previous * 2, _ENDED_PROBE_MAX_DELAY_SECONDS)
            retry_at[scanned.run_id] = (time.monotonic() + delay, delay)
        else:
            retry_at.pop(scanned.run_id, None)


async def run_completion_watcher(app: FastAPI) -> None:
    """Async loop started from the FastAPI lifespan; see the module docstring.

    Without Redis there are no end markers to watch, and the stale-run sweeper
    alone settles headless runs.
    """
    if not app.state.settings.redis_url:
        logger.info("completion watcher: no Redis configured; not started")
        return
    await asyncio.sleep(_WATCH_STARTUP_DELAY_SECONDS)
    logger.info("completion watcher: starting (interval=%.1fs)", COMPLETION_POLL_SECONDS)
    retry_at: dict[str, tuple[float, float]] = {}
    while True:
        try:
            await _watch_once(app, retry_at)
        except asyncio.CancelledError:
            raise
        except Exception:  # keep the loop alive; the next tick retries
            logger.exception("completion watcher: tick failed")
        await asyncio.sleep(COMPLETION_POLL_SECONDS)
