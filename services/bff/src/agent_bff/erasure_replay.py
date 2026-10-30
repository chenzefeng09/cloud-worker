"""Re-apply GDPR erasures after a backup restore.

A restored backup holds the data of every user erased after it was taken.
Before traffic reaches a restored stack, run inside the BFF container (so the
command uses the BFF's own configuration):

    python -m agent_bff.erasure_replay --since 2026-06-05T10:20:00Z --actor usr_…

It reads every erasure the ledger (``domain/erasure_ledger.py``) recorded at
or after ``--since`` — the restore point less a few minutes, for erasures
still running at that point (restore runbook §3.7) — and erases each recorded
user again with the admin erasure fan-out (``privacy.run_erasure``: a preflight
that changes nothing, then schedules, platform, memories, LLM preferences,
Apps and BFF rows; every step idempotent), acting as ``--actor`` with
``X-Actor: ADMIN`` inside the user's own tenant.  A user recorded more
than once is erased once, in full: data created after the request goes too,
so ``--since`` should reach back no further than that.  Every replayed user
gets an ``admin.privacy.user.erasure_replayed`` audit event (counts only).

Prints a JSON report with one result per user and exits with

    0  every recorded user was erased again (``--dry-run``: nothing erased)
    1  at least one erasure is incomplete — fix the failing service and rerun
       (``"phase": "preflight"``: that user's preflight failed, nothing erased)
    2  bad arguments, or the ledger is not configured or can't be read
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sys
from datetime import datetime
from typing import Any

from agent_bff.api.v1.privacy import (
    USER_ID_PATTERN,
    DataSubject,
    ErasureIncompleteError,
    ErasureRefusedError,
    emit_privacy_audit,
    erasure_counts,
    run_erasure,
)
from agent_bff.config import Settings
from agent_bff.domain.db import close_db, init_db
from agent_bff.domain.erasure_ledger import ErasureLedger, LedgerEntry, LedgerError

REPLAY_EVENT = "admin.privacy.user.erasure_replayed"

EXIT_OK = 0
EXIT_INCOMPLETE = 1
EXIT_UNUSABLE = 2


async def _replay_one(
    settings: Settings, subject: DataSubject, since: datetime,
) -> dict[str, Any]:
    """Erase one recorded user again and audit it; never raises."""
    try:
        run = await run_erasure(settings, subject)
    except ErasureIncompleteError as exc:
        result: dict[str, Any] = {
            "status": "incomplete",
            "succeeded": exc.succeeded,
            "skipped": exc.skipped,
            "failed": exc.failed,
            "not_attempted": exc.not_attempted,
            "error": exc.error,
            "counts": erasure_counts(exc.results),
        }
        if isinstance(exc, ErasureRefusedError):
            result["phase"] = "preflight"
        outcome = "PARTIAL" if exc.succeeded else "FAILURE"
    else:
        result = {
            "status": "erased",
            "succeeded": run.succeeded,
            "skipped": run.skipped,
            "counts": erasure_counts(run.results),
        }
        outcome = "SUCCESS"
    payload: dict[str, Any] = {
        "via": "restore_replay",
        "since": since.isoformat(),
        "succeeded": result["succeeded"],
        **result["counts"],
    }
    if "failed" in result:
        payload["failed"] = result["failed"]
    if "phase" in result:
        payload["phase"] = result["phase"]
    if result["skipped"]:
        payload["skipped"] = result["skipped"]
    await emit_privacy_audit(
        settings, subject, event_type=REPLAY_EVENT, outcome=outcome, payload=payload,
    )
    return result


async def replay_erasures(
    settings: Settings,
    ledger: ErasureLedger,
    *,
    since: datetime,
    actor_user_id: str,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Erase again every user the ledger recorded at or after ``since``.

    Users are replayed one at a time, oldest request first.  Raises
    ``LedgerError`` when the ledger can't be read — then nothing is erased.
    """
    entries = await ledger.entries_since(since)
    by_user: dict[tuple[str, str], list[LedgerEntry]] = {}
    for entry in entries:
        by_user.setdefault((entry.tenant_id, entry.user_id), []).append(entry)

    results: list[dict[str, Any]] = []
    for (tenant_id, user_id), recorded in by_user.items():
        result: dict[str, Any] = {
            "tenant_id": tenant_id,
            "user_id": user_id,
            "requests": len(recorded),
            "last_requested_at": recorded[-1].requested_at.isoformat(),
        }
        if dry_run:
            result["status"] = "pending"
        else:
            subject = DataSubject(
                tenant_id=tenant_id,
                user_id=user_id,
                actor_user_id=actor_user_id,
                access_token="",
                admin=True,
            )
            result.update(await _replay_one(settings, subject, since))
        results.append(result)

    return {
        "since": since.isoformat(),
        "dry_run": dry_run,
        "ledger_entries": len(entries),
        "users": len(results),
        "erased": sum(r["status"] == "erased" for r in results),
        "incomplete": sum(r["status"] == "incomplete" for r in results),
        "results": results,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Command line
# ─────────────────────────────────────────────────────────────────────────────

def _since(value: str) -> datetime:
    try:
        moment = datetime.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not an ISO-8601 time: {value!r}") from exc
    if moment.tzinfo is None:
        raise argparse.ArgumentTypeError(
            f"{value!r} has no time zone; add one, e.g. 2026-06-05T10:30:00Z",
        )
    return moment


def _actor(value: str) -> str:
    if not re.fullmatch(USER_ID_PATTERN, value):
        raise argparse.ArgumentTypeError(f"not a user id (usr_…): {value!r}")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m agent_bff.erasure_replay",
        description=(
            "After a backup restore, erase again every user whose GDPR erasure the "
            "ledger recorded at or after --since.  Run it before reopening traffic."
        ),
    )
    parser.add_argument(
        "--since",
        required=True,
        type=_since,
        help=(
            "replay erasures recorded at or after this time: the restore point less a few "
            "minutes, ISO-8601 with a time zone (e.g. 2026-06-05T10:20:00Z)"
        ),
    )
    parser.add_argument(
        "--actor",
        required=True,
        type=_actor,
        help="user id (usr_…) of the administrator running the replay, for the audit trail",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="list the users that would be erased again, erase nothing",
    )
    return parser


async def _run(settings: Settings, args: argparse.Namespace) -> int:
    ledger = ErasureLedger.from_settings(settings)
    if ledger is None:
        print(
            "error: no erasure ledger is configured (BFF_PRIVACY_LEDGER_S3_BUCKET)",
            file=sys.stderr,
        )
        return EXIT_UNUSABLE
    await init_db(settings)
    try:
        report = await replay_erasures(
            settings,
            ledger,
            since=args.since,
            actor_user_id=args.actor,
            dry_run=args.dry_run,
        )
    except LedgerError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_UNUSABLE
    finally:
        await close_db()
    print(json.dumps(report, indent=2))
    return EXIT_INCOMPLETE if report["incomplete"] else EXIT_OK


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    return asyncio.run(_run(Settings(), args))


if __name__ == "__main__":
    sys.exit(main())
