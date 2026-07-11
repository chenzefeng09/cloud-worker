"""Background reconcile sweeper — persists terminal state for HEADLESS runs.

Problem
-------
``_reconcile_run_completion`` in ``api.v1.messages`` is called only when a
client actively polls the session (SSE stream or GET /messages).  Runs
triggered by the cron-runner (``source='schedule'``) have no browser viewer:
the sandbox exits, ``result.json`` is written, but the ``Run`` row stays
``RUNNING`` / ``PENDING`` forever.

Solution
--------
A background asyncio loop runs every ``SWEEP_INTERVAL_SECONDS`` seconds.  It
finds every run across all tenants whose status is still ``RUNNING`` or
``PENDING`` and whose age exceeds ``STALE_AFTER_SECONDS``, then calls the same
``_reconcile_run_completion`` helper that the normal poll path calls.

Cross-tenant read
-----------------
RLS is ``FORCE``-enabled on ``app_user``, so a plain ``get_session`` would
return zero rows for any tenant other than the one bound to the current
transaction.  Following the exact pattern established by
``services.session_service.purge_expired_sessions``, the stale-run scan uses
``domain.db.privileged_scalars`` — a short-lived connection to the
``app_migrator`` (``BYPASSRLS``) DSN that returns the raw
``(tenant_id, run_id, session_id)`` tuples.  Each *write* (reconcile) then
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
import datetime as dt
import logging

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

# Inline (chat-mode) runs have no sandbox and no scheduler container, so the
# scheduler reconciler can never resolve them — a process restart mid-stream
# would otherwise leave the run RUNNING forever.  After this window an inline
# run still stuck RUNNING/PENDING is force-FAILED (its executor task died with
# the process; the registry that held its handle is gone).  Generous so a
# genuinely slow reasoning completion is never killed out from under a live
# executor — that completes on its own and finalizes the row before this fires.
INLINE_ORPHAN_AFTER_SECONDS: int = 20 * 60

# Short startup delay so a fleet of pods restarting together does not all hit
# the DB at the same instant.
_SWEEP_STARTUP_DELAY_SECONDS: int = 30


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
            from agent_session_orchestrator.services.inline_run_executor import (
                _push_end,  # noqa: PLC0415
            )

            await _push_end(redis_url, run_obj.id)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 — end marker is best-effort
            logger.debug(
                "reconcile_sweeper: end marker push failed for orphaned inline run",
                extra={"run_id": getattr(run_obj, "id", "?")},
            )


async def _sweep_once(app: FastAPI) -> None:
    """One sweep iteration: find stale runs and reconcile each one."""
    settings = app.state.settings
    cutoff = dt.datetime.now(tz=dt.UTC) - dt.timedelta(seconds=STALE_AFTER_SECONDS)

    # ── 1. Cross-tenant scan (BYPASSRLS migrator connection) ───────────────
    # We encode four values as a single string to fit the scalar helper's
    # one-column contract: "tenant_id\x1frun_id\x1fsession_id\x1fuser_id".
    #
    # The session.user_id is carried out of the scan directly via a LEFT JOIN
    # on session_orchestrator.session.  A LEFT JOIN (not INNER) is deliberate:
    # a session soft-deleted by the 30-day retention sweep only has its
    # ``deleted_at`` column set — the row still exists — so the JOIN resolves
    # its user_id even though the tenant-bound ``get_session`` path (which
    # filters ``deleted_at IS NULL``) would raise SessionNotFoundError for it.
    # Without this, orphaned runs of soft-deleted sessions fell back to a
    # ``SYSTEM`` user_id, which the scheduler rejects with 403 → run stuck
    # RUNNING forever.  COALESCE guards the (rare) case of a run whose session
    # row is genuinely gone, yielding an empty string handled below.
    rows = await privileged_scalars(
        settings,
        "SELECT r.tenant_id || '\x1f' || r.id || '\x1f' || r.session_id"
        "       || '\x1f' || COALESCE(s.user_id, '')"
        "  FROM session_orchestrator.run r"
        "  LEFT JOIN session_orchestrator.session s ON s.id = r.session_id"
        " WHERE r.status IN ('RUNNING', 'PENDING')"
        "   AND COALESCE(r.started_at, r.created_at) < :cutoff"
        " ORDER BY r.tenant_id",
        {"cutoff": cutoff},
    )

    if not rows:
        logger.debug("reconcile_sweeper: no stale runs found")
        return

    logger.info("reconcile_sweeper: found %d stale run(s) to probe", len(rows))

    # Lazy import to avoid circular imports at module load time.
    from agent_session_orchestrator.api.v1.messages import (
        _reconcile_run_completion,  # noqa: PLC0415
    )

    scheduler_url: str = settings.agent_scheduler_url
    knowledge_ingest_url: str = settings.knowledge_ingest_url
    redis_url: str | None = settings.redis_url
    dlp_url: str = settings.foundation_dlp_url

    # ── 2. Reconcile each run in its own tenant-bound session ──────────────
    for row in rows:
        parts = str(row).split("\x1f", 3)
        if len(parts) != 4:
            logger.warning("reconcile_sweeper: malformed scan row %r — skipping", row)
            continue
        tenant_id, run_id, session_id, scan_user_id = parts
        # user_id may legitimately be an empty string (session row genuinely
        # gone); only the first three fields are required to identify a run.
        if not tenant_id or not run_id or not session_id:
            logger.warning(
                "reconcile_sweeper: empty field in scan row %r — skipping", row
            )
            continue

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
                user_id = scan_user_id
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
                    continue

                # ── Inline (chat-mode) orphan branch ───────────────────────
                # An inline run (execution_mode='inline', sandbox_id IS NULL) is
                # never resolvable via the scheduler.  If it is still stuck past
                # the (much larger) inline window, its in-process executor task
                # died with a restarted process — force it FAILED and push the
                # ``end`` marker so any live SSE tail closes.  Deliberately does
                # NOT touch sandbox runs (which take the reconcile path below).
                if (
                    run_obj.execution_mode == "inline"
                    and run_obj.sandbox_id is None
                ):
                    if _inline_run_is_orphaned(run_obj):
                        await _fail_orphaned_inline_run(
                            run_obj=run_obj, redis_url=redis_url,
                        )
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
                    continue

                terminal, _ = await _reconcile_run_completion(
                    scheduler_url=scheduler_url,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    run=run_obj,
                    svc=svc,
                    knowledge_ingest_url=knowledge_ingest_url,
                    redis_url=redis_url,
                    dlp_url=dlp_url,
                )
                if terminal:
                    logger.info(
                        "reconcile_sweeper: run reconciled to terminal state %s",
                        run_obj.status,
                        extra={"run_id": run_id, "tenant_id": tenant_id},
                    )

        except asyncio.CancelledError:
            raise  # propagate shutdown signal
        except Exception:  # noqa: BLE001
            logger.exception(
                "reconcile_sweeper: error reconciling run",
                extra={"tenant_id": tenant_id, "run_id": run_id},
            )


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
