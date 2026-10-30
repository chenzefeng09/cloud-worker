"""Tests: replaying erasures after a backup restore (``agent_bff.erasure_replay``).

Driven through ``main()`` — the command the restore runbook gives — against
the fake ledger bucket, the fake platform and a file-backed SQLite database
(the command opens its own DB connection, like it does in the container).

Covers:
1. Every user recorded at or after ``--since`` is erased again, once, as the
   actor with ``X-Actor: ADMIN`` inside the user's own tenant; earlier entries
   are left alone; one result and one audit event (counts only) per user.
2. An incomplete user → exit 1 naming what finished; a rerun after the fix
   completes (every step idempotent).
3. ``--dry-run`` lists the users and erases nothing.
4. No ledger, an unreadable ledger or bad arguments → exit 2, nothing erased.
5. End to end: an erasure recorded by the API is erased again after a restore
   brings the data back.
"""

from __future__ import annotations

import asyncio
import copy
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from agent_bff import erasure_replay
from agent_bff.config import Settings
from agent_bff.domain.models import (
    ApiKey,
    Base,
    NotificationPreference,
    PrivacyNoticeAcknowledgement,
)
from agent_bff.main import create_app
from tests.privacy_fakes import (
    ADMIN,
    LEDGER_BUCKET,
    MEMORY_TEXT,
    OTHER_TENANT,
    PREFIX,
    TENANT,
    USER_A,
    USER_B,
    FakePlatform,
    FakeS3,
    add_ledger_entry,
    assert_valid_audit_event,
    install_fake,
    install_ledger,
    make_cookie,
    make_settings,
)

USER_C = "usr_01J5TEST0000000000000USER3"
SINCE = datetime(2026, 10, 30, 8, 0, tzinfo=UTC)
REPLAY = ("--since", "2026-10-30T08:00:00Z", "--actor", ADMIN)
ADMIN_STEPS = ["schedules", "platform", "memories", "bff"]
SEEDED_BFF = {"notification_preferences": 1, "api_keys": 0, "privacy_notice_acknowledgements": 1}


# ── BFF database (a file, so every event loop sees the same data) ─────────────

def _with_db[T](dsn: str, fn: Callable[[AsyncSession], Awaitable[T]]) -> T:
    async def _go() -> T:
        engine = create_async_engine(dsn)
        try:
            factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
            async with factory() as session, session.begin():
                return await fn(session)
        finally:
            await engine.dispose()

    return asyncio.run(_go())


def seed(dsn: str, user_id: str, tenant_id: str) -> None:
    """One notification preference and one notice acknowledgement."""

    async def _add(session: AsyncSession) -> None:
        session.add(NotificationPreference(
            tenant_id=tenant_id, user_id=user_id, event_key="task_completed",
            email_enabled=False, push_enabled=True,
        ))
        session.add(PrivacyNoticeAcknowledgement(
            tenant_id=tenant_id, user_id=user_id, notice_version="2026-10.bbbbbbbbbbbb",
        ))

    _with_db(dsn, _add)


def bff_rows(dsn: str, user_id: str, tenant_id: str) -> int:
    async def _count(session: AsyncSession) -> int:
        total = 0
        for model in (NotificationPreference, ApiKey, PrivacyNoticeAcknowledgement):
            total += (
                await session.execute(
                    select(func.count())
                    .select_from(model)
                    .where(model.tenant_id == tenant_id, model.user_id == user_id)
                )
            ).scalar_one()
        return total

    return _with_db(dsn, _count)


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def dsn(tmp_path: Path) -> str:
    dsn = f"sqlite+aiosqlite:///{tmp_path / 'bff.db'}"

    async def _create() -> None:
        engine = create_async_engine(dsn)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await engine.dispose()

    asyncio.run(_create())
    return dsn


@pytest.fixture
def settings(dsn: str) -> Settings:
    return make_settings(db_dsn=dsn, privacy_ledger_s3_bucket=LEDGER_BUCKET)


@pytest.fixture(autouse=True)
def cli_settings(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> None:
    """The command reads ``Settings()`` from the environment; use the test's."""
    monkeypatch.setattr(erasure_replay, "Settings", lambda: settings)


@pytest.fixture
def s3(monkeypatch: pytest.MonkeyPatch) -> FakeS3:
    return install_ledger(monkeypatch, FakeS3())


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakePlatform:
    return install_fake(monkeypatch, FakePlatform())


def run_cli(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, dict[str, Any]]:
    code = erasure_replay.main(list(argv))
    return code, json.loads(capsys.readouterr().out)


# ── Replay ────────────────────────────────────────────────────────────────────

def test_replay_erases_every_user_recorded_since_the_restore_point(
    capsys: pytest.CaptureFixture[str], dsn: str, s3: FakeS3, fake: FakePlatform,
) -> None:
    # USER_C's erasure is older than the backup, so the backup has it already.
    add_ledger_entry(s3, at=SINCE - timedelta(minutes=5), tenant_id=TENANT, user_id=USER_C)
    add_ledger_entry(s3, at=SINCE + timedelta(minutes=1), tenant_id=TENANT, user_id=USER_A)
    add_ledger_entry(
        s3, at=SINCE + timedelta(minutes=2), tenant_id=OTHER_TENANT, user_id=USER_B,
        via="admin_dsar", actor_user_id="usr_01J5TEST000000000000ADMIN2",
    )
    last_a = add_ledger_entry(s3, at=SINCE + timedelta(minutes=3), tenant_id=TENANT, user_id=USER_A)
    seed(dsn, USER_A, TENANT)
    seed(dsn, USER_B, OTHER_TENANT)
    seed(dsn, USER_C, TENANT)

    code, report = run_cli(capsys, *REPLAY)

    assert code == 0
    assert report["since"] == "2026-10-30T08:00:00+00:00"
    assert report["dry_run"] is False
    assert (report["ledger_entries"], report["users"]) == (3, 2)
    assert (report["erased"], report["incomplete"]) == (2, 0)
    first, second = report["results"]
    assert (first["tenant_id"], first["user_id"], first["requests"]) == (TENANT, USER_A, 2)
    assert first["last_requested_at"] == last_a.requested_at.isoformat()
    assert (second["tenant_id"], second["user_id"], second["requests"]) == (OTHER_TENANT, USER_B, 1)
    for result in (first, second):
        assert result["status"] == "erased"
        assert result["succeeded"] == ADMIN_STEPS
        assert result["skipped"] == {"llm_preferences": "not_supported"}
        assert result["counts"]["bff"] == SEEDED_BFF
    assert first["counts"]["schedules_deleted"] == 2
    assert first["counts"]["memories_deleted"] == 2
    assert first["counts"]["platform_deleted"] == {"sessions": 2, "messages": 7, "workspaces": 1}

    # The admin fan-out, inside each user's own tenant, as the actor (no bearer).
    orch_a, orch_b = fake.calls_to("orchestrator")
    assert (orch_a.headers["X-Tenant-Id"], orch_a.json) == (
        TENANT, {"dry_run": False, "user_id": USER_A},
    )
    assert (orch_b.headers["X-Tenant-Id"], orch_b.json) == (
        OTHER_TENANT, {"dry_run": False, "user_id": USER_B},
    )
    assert [c.path for c in fake.calls_to("knowledge")] == [
        f"/internal/privacy/users/{USER_A}/memories",
        f"/internal/privacy/users/{USER_B}/memories",
    ]
    for call in (*fake.calls_to("orchestrator"), *fake.calls_to("knowledge")):
        assert call.headers["X-Actor"] == "ADMIN"
        assert call.headers["X-User-Id"] == ADMIN
        assert "Authorization" not in call.headers
    assert [c.headers["X-Tenant-Id"] for c in fake.calls_to("cron")] == [TENANT, OTHER_TENANT]
    assert fake.calls_to("llm") == []

    # Erased again; the user whose erasure the backup already holds is untouched.
    assert bff_rows(dsn, USER_A, TENANT) == 0
    assert bff_rows(dsn, USER_B, OTHER_TENANT) == 0
    assert bff_rows(dsn, USER_C, TENANT) == 2
    assert USER_A not in fake.memories
    assert USER_B not in fake.memories
    assert not any(USER_C in json.dumps([c.path, c.json]) for c in fake.calls)


def test_each_replayed_user_is_audited_as_the_actor_with_counts_only(
    capsys: pytest.CaptureFixture[str], dsn: str, s3: FakeS3, fake: FakePlatform,
) -> None:
    add_ledger_entry(s3, at=SINCE + timedelta(minutes=1), tenant_id=TENANT, user_id=USER_A)
    add_ledger_entry(s3, at=SINCE + timedelta(minutes=2), tenant_id=OTHER_TENANT, user_id=USER_B)

    run_cli(capsys, *REPLAY)

    assert [(e["tenant_id"], e["resource_id"]) for e in fake.audit_events] == [
        (TENANT, USER_A), (OTHER_TENANT, USER_B),
    ]
    for event in fake.audit_events:
        assert_valid_audit_event(event)
        assert event["event_type"] == "admin.privacy.user.erasure_replayed"
        assert event["outcome"] == "SUCCESS"
        assert (event["actor_kind"], event["actor_user_id"]) == ("ADMIN", ADMIN)
        assert event["resource_type"] == "user"
        assert event["payload"]["via"] == "restore_replay"
        assert event["payload"]["since"] == "2026-10-30T08:00:00+00:00"
        assert event["payload"]["succeeded"] == ADMIN_STEPS
        assert event["payload"]["skipped"] == {"llm_preferences": "not_supported"}
        assert MEMORY_TEXT not in json.dumps(event)


def test_an_incomplete_user_exits_1_and_a_rerun_finishes(
    capsys: pytest.CaptureFixture[str], dsn: str, s3: FakeS3, fake: FakePlatform,
) -> None:
    add_ledger_entry(s3, at=SINCE + timedelta(minutes=1), tenant_id=TENANT, user_id=USER_A)
    seed(dsn, USER_A, TENANT)
    fake.down.add("knowledge")

    code, report = run_cli(capsys, *REPLAY)

    assert code == 1
    assert (report["erased"], report["incomplete"]) == (0, 1)
    [result] = report["results"]
    assert result["status"] == "incomplete"
    assert result["succeeded"] == ["schedules", "platform"]
    assert result["failed"] == "memories"
    assert result["not_attempted"] == ["llm_preferences", "bff"]
    assert "knowledge-ingest unreachable" in result["error"]
    assert bff_rows(dsn, USER_A, TENANT) == 2  # the BFF step never ran
    [event] = fake.audit_events
    assert event["outcome"] == "PARTIAL"
    assert event["payload"]["failed"] == "memories"

    # Every step is idempotent: once knowledge-ingest is back, a rerun finishes.
    fake.down.clear()
    code, report = run_cli(capsys, *REPLAY)

    assert code == 0
    [result] = report["results"]
    assert result["status"] == "erased"
    assert result["counts"]["schedules_deleted"] == 0  # already gone
    assert result["counts"]["memories_deleted"] == 2
    assert bff_rows(dsn, USER_A, TENANT) == 0


def test_dry_run_lists_the_users_and_erases_nothing(
    capsys: pytest.CaptureFixture[str], dsn: str, s3: FakeS3, fake: FakePlatform,
) -> None:
    add_ledger_entry(s3, at=SINCE + timedelta(minutes=1), tenant_id=TENANT, user_id=USER_A)
    seed(dsn, USER_A, TENANT)

    code, report = run_cli(capsys, *REPLAY, "--dry-run")

    assert code == 0
    assert report["dry_run"] is True
    assert [(r["user_id"], r["status"]) for r in report["results"]] == [(USER_A, "pending")]
    assert fake.calls == []
    assert bff_rows(dsn, USER_A, TENANT) == 2


# ── Unusable ledger / arguments ───────────────────────────────────────────────

def test_without_a_ledger_the_replay_refuses_to_run(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, dsn: str,
    fake: FakePlatform,
) -> None:
    monkeypatch.setattr(erasure_replay, "Settings", lambda: make_settings(db_dsn=dsn))

    code = erasure_replay.main(list(REPLAY))

    assert code == 2
    captured = capsys.readouterr()
    assert "BFF_PRIVACY_LEDGER_S3_BUCKET" in captured.err
    assert captured.out == ""
    assert fake.calls == []


def test_an_unreadable_ledger_erases_nothing(
    capsys: pytest.CaptureFixture[str], dsn: str, s3: FakeS3, fake: FakePlatform,
) -> None:
    add_ledger_entry(s3, at=SINCE + timedelta(minutes=1), tenant_id=TENANT, user_id=USER_A)
    s3.objects["erasures/20261030T090000.000000Z-broken.json"] = b"{"

    code = erasure_replay.main(list(REPLAY))

    assert code == 2
    assert "broken" in capsys.readouterr().err
    assert fake.calls == []


@pytest.mark.parametrize("argv", [
    ["--since", "2026-10-30T08:00:00", "--actor", ADMIN],  # no time zone
    ["--since", "yesterday", "--actor", ADMIN],
    ["--since", "2026-10-30T08:00:00Z", "--actor", "admin@example.com"],
    ["--actor", ADMIN],
    ["--since", "2026-10-30T08:00:00Z"],
])
def test_bad_arguments_exit_2(
    argv: list[str], s3: FakeS3, fake: FakePlatform,
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        erasure_replay.main(argv)

    assert exc_info.value.code == 2
    assert fake.calls == []


# ── End to end ────────────────────────────────────────────────────────────────

def test_an_erasure_recorded_by_the_api_is_replayed_after_a_restore(
    capsys: pytest.CaptureFixture[str], settings: Settings, dsn: str, s3: FakeS3,
    fake: FakePlatform,
) -> None:
    seed(dsn, USER_A, TENANT)
    backup = {
        "memories": copy.deepcopy(fake.memories[USER_A]),
        "schedules": copy.deepcopy(fake.schedules[USER_A]),
    }
    backup_taken_at = datetime.now(UTC)

    with TestClient(create_app(settings), raise_server_exceptions=False) as client:
        resp = client.post(
            PREFIX + "/me/privacy/erase",
            json={"confirm": "DELETE MY DATA"},
            cookies=make_cookie(settings, USER_A),
        )
    assert resp.status_code == 200, resp.text
    [entry] = s3.entries()
    assert (entry["user_id"], entry["via"]) == (USER_A, "self_service")
    assert bff_rows(dsn, USER_A, TENANT) == 0

    # Restoring the backup brings USER_A's data back …
    seed(dsn, USER_A, TENANT)
    fake.memories[USER_A] = backup["memories"]
    fake.schedules[USER_A] = backup["schedules"]

    # … and the replay erases it again.
    code, report = run_cli(
        capsys, "--since", backup_taken_at.isoformat(), "--actor", ADMIN,
    )

    assert code == 0
    [result] = report["results"]
    assert (result["tenant_id"], result["user_id"], result["status"]) == (
        TENANT, USER_A, "erased",
    )
    assert result["counts"]["memories_deleted"] == 2
    assert result["counts"]["schedules_deleted"] == 2
    assert bff_rows(dsn, USER_A, TENANT) == 0
    assert USER_A not in fake.memories
    assert USER_A not in fake.schedules
