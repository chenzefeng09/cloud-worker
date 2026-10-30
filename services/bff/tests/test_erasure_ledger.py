"""Tests: the erasure ledger (``domain/erasure_ledger.py``) and the erase routes.

Covers:
1. Entries — one Object-Locked JSON object per request holding ids and the
   request time only; listing from a restore point in time order, across
   pages; a listing or entry that can't be read is an error, never a skip.
2. Erase routes (self-service + admin) — the entry is written before any
   erasure step runs; a ledger that can't be written refuses the erasure
   (503, audited, nothing erased).  Without a configured ledger the erasure
   still runs (covered in ``test_privacy_api.py``: ``"ledger": "not_configured"``).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta, timezone

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_bff.config import Settings
from agent_bff.domain.erasure_ledger import ErasureLedger, LedgerError
from tests.privacy_fakes import (
    ADMIN,
    LEDGER_BUCKET,
    NO_ROWS,
    PREFIX,
    SEEDED,
    TENANT,
    USER_A,
    USER_B,
    FakePlatform,
    FakeS3,
    add_ledger_entry,
    app_client,
    assert_valid_audit_event,
    count_rows,
    install_fake,
    install_ledger,
    make_cookie,
    make_settings,
    seed_rows,
    sqlite_factory,
)

SINCE = datetime(2026, 10, 30, 8, 0, tzinfo=UTC)
CEST = timezone(timedelta(hours=2))

SELF_ERASE = ("/me/privacy/erase", {"confirm": "DELETE MY DATA"}, USER_A, "self_service")
ADMIN_ERASE = (f"/admin/privacy/users/{USER_A}/erase", {"confirm": USER_A}, ADMIN, "admin_dsar")


def make_ledger() -> ErasureLedger:
    return ErasureLedger(bucket=LEDGER_BUCKET, retention_days=30)


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def s3(monkeypatch: pytest.MonkeyPatch) -> FakeS3:
    return install_ledger(monkeypatch, FakeS3())


@pytest.fixture
def settings() -> Settings:
    return make_settings(privacy_ledger_s3_bucket=LEDGER_BUCKET)


@pytest_asyncio.fixture
async def db_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with sqlite_factory() as factory:
        yield factory


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakePlatform:
    return install_fake(monkeypatch, FakePlatform())


@pytest.fixture
def client(
    settings: Settings,
    db_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    with app_client(settings, monkeypatch) as c:
        yield c


# ── Entries ───────────────────────────────────────────────────────────────────

async def test_record_stores_one_locked_entry_with_ids_and_time_only(s3: FakeS3) -> None:
    before = datetime.now(UTC)

    entry = await make_ledger().record(
        tenant_id=TENANT, user_id=USER_A, via="admin_dsar", actor_user_id=ADMIN,
    )

    assert before <= entry.requested_at <= datetime.now(UTC)
    [put] = s3.puts
    assert put["Key"] == entry.key
    assert put["Key"].startswith("erasures/")
    assert put["Key"].endswith(f"-{entry.entry_id}.json")
    assert put["ContentType"] == "application/json"
    assert put["ObjectLockMode"] == "COMPLIANCE"
    assert put["ObjectLockRetainUntilDate"] == entry.requested_at + timedelta(days=30)
    assert json.loads(put["Body"]) == {
        "version": 1,
        "entry_id": entry.entry_id,
        "requested_at": entry.requested_at.isoformat(),
        "tenant_id": TENANT,
        "user_id": USER_A,
        "via": "admin_dsar",
        "actor_user_id": ADMIN,
    }


async def test_a_failed_write_is_a_ledger_error(s3: FakeS3) -> None:
    s3.fail = ConnectionError("object store unreachable")

    with pytest.raises(LedgerError, match="ConnectionError"):
        await make_ledger().record(
            tenant_id=TENANT, user_id=USER_A, via="self_service", actor_user_id=USER_A,
        )


async def test_entries_since_lists_from_the_restore_point_in_time_order(s3: FakeS3) -> None:
    s3.page_size = 2  # force pagination
    add_ledger_entry(s3, at=SINCE - timedelta(microseconds=1), tenant_id=TENANT, user_id=USER_B)
    on_time = add_ledger_entry(s3, at=SINCE, tenant_id=TENANT, user_id=USER_A)
    later = add_ledger_entry(s3, at=SINCE + timedelta(hours=2), tenant_id=TENANT, user_id=USER_B)
    soon = add_ledger_entry(s3, at=SINCE + timedelta(minutes=1), tenant_id=TENANT, user_id=USER_A)
    ledger = make_ledger()

    assert await ledger.entries_since(SINCE) == [on_time, soon, later]
    # Same instant in another zone → same entries.
    assert await ledger.entries_since(SINCE.astimezone(CEST)) == [on_time, soon, later]
    assert await ledger.entries_since(SINCE + timedelta(days=1)) == []


async def test_an_unreadable_entry_fails_the_listing(s3: FakeS3) -> None:
    add_ledger_entry(s3, at=SINCE, tenant_id=TENANT, user_id=USER_A)
    s3.objects["erasures/20261030T090000.000000Z-broken.json"] = b'{"user_id": "usr_x"}'

    with pytest.raises(LedgerError, match="broken"):
        await make_ledger().entries_since(SINCE)


async def test_a_listing_failure_is_a_ledger_error(s3: FakeS3) -> None:
    s3.fail = ConnectionError("object store unreachable")

    with pytest.raises(LedgerError):
        await make_ledger().entries_since(SINCE)


async def test_entries_since_needs_a_time_zone(s3: FakeS3) -> None:
    with pytest.raises(ValueError, match="timezone"):
        await make_ledger().entries_since(SINCE.replace(tzinfo=None))


def test_a_ledger_exists_only_when_a_bucket_is_configured() -> None:
    assert ErasureLedger.from_settings(make_settings()) is None
    assert ErasureLedger.from_settings(
        make_settings(privacy_ledger_s3_bucket=LEDGER_BUCKET),
    ) is not None


# ── Erase routes ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("path,body,caller,via", [SELF_ERASE, ADMIN_ERASE])
def test_erasure_is_recorded_before_any_step_runs(
    client: TestClient, settings: Settings, db_factory, fake: FakePlatform, s3: FakeS3,
    path: str, body: dict, caller: str, via: str,
) -> None:
    seed_rows(client, db_factory, USER_A)
    writes_at_record: list[list[tuple[str, str, str]]] = []
    s3.on_put = lambda: writes_at_record.append(fake.writes())

    resp = client.post(PREFIX + path, json=body, cookies=make_cookie(settings, caller))

    assert resp.status_code == 200, resp.text
    # After the preflight (reads and dry runs only), before anything is erased.
    assert writes_at_record == [[]]
    [entry] = s3.entries()
    assert (entry["tenant_id"], entry["user_id"]) == (TENANT, USER_A)
    assert (entry["via"], entry["actor_user_id"]) == (via, caller)
    assert count_rows(client, db_factory, USER_A) == NO_ROWS
    [event] = fake.audit_events
    assert event["outcome"] == "SUCCESS"
    assert event["payload"]["ledger"] == "recorded"


@pytest.mark.parametrize("path,body,caller,via", [SELF_ERASE, ADMIN_ERASE])
def test_a_refused_preflight_records_nothing_in_the_ledger(
    client: TestClient, settings: Settings, db_factory, fake: FakePlatform, s3: FakeS3,
    path: str, body: dict, caller: str, via: str,
) -> None:
    """Nothing was erased, so there is nothing for a restore to replay."""
    seed_rows(client, db_factory, USER_A)
    fake.down.add("apps")

    resp = client.post(PREFIX + path, json=body, cookies=make_cookie(settings, caller))

    assert resp.status_code == 503, resp.text
    assert resp.json()["detail"]["code"] == "PRIVACY_ERASURE_UNAVAILABLE"
    assert s3.entries() == []
    assert fake.writes() == []


@pytest.mark.parametrize("path,body,caller,via", [SELF_ERASE, ADMIN_ERASE])
def test_erasure_is_refused_when_the_ledger_cannot_record_it(
    client: TestClient, settings: Settings, db_factory, fake: FakePlatform, s3: FakeS3,
    path: str, body: dict, caller: str, via: str,
) -> None:
    seed_rows(client, db_factory, USER_A)
    s3.fail = ConnectionError("object store unreachable")

    resp = client.post(PREFIX + path, json=body, cookies=make_cookie(settings, caller))

    assert resp.status_code == 503, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == "PRIVACY_LEDGER_UNAVAILABLE"
    assert "Nothing was erased" in detail["user_message"]
    assert fake.writes() == []
    assert count_rows(client, db_factory, USER_A) == SEEDED
    assert USER_A in fake.memories
    assert USER_A in fake.schedules
    [event] = fake.audit_events
    assert_valid_audit_event(event)
    assert event["outcome"] == "FAILURE"
    assert event["resource_id"] == USER_A
    assert event["payload"] == {"via": via, "failed": "ledger"}

    # Once the ledger is reachable again the same request goes through.
    s3.fail = None
    retry = client.post(PREFIX + path, json=body, cookies=make_cookie(settings, caller))

    assert retry.status_code == 200, retry.text
    assert len(s3.entries()) == 1
    assert count_rows(client, db_factory, USER_A) == NO_ROWS
