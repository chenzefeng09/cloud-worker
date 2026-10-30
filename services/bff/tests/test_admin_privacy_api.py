"""Tests: tenant-admin data-subject requests (``api/v1/admin_privacy.py``).

Covers:
1. Login + tenant-admin guard (fail-closed) before any data is touched.
2. Admin export — the subject's data only, ``X-Actor: ADMIN`` with the
   subject named explicitly, download headers, audit event (counts only).
3. Admin erase — typed user-id confirmation, a preflight that touches nothing,
   subject-only and tenant-bound deletion (model preferences and Apps
   included), refusal and partial failure, audit events.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_bff.api.v1.admin_privacy import ADMIN_ACCOUNT_NOTE
from agent_bff.config import Settings
from tests.privacy_fakes import (
    ADMIN,
    MEMORY_TEXT,
    NO_ROWS,
    OTHER_TENANT,
    PREFIX,
    SEEDED,
    TENANT,
    USER_A,
    USER_B,
    FakePlatform,
    app_client,
    assert_valid_audit_event,
    count_rows,
    install_fake,
    make_cookie,
    make_settings,
    seed_rows,
    sqlite_factory,
)

MEMBER = USER_A
EXPORT = f"/admin/privacy/users/{MEMBER}/export"
ERASE = f"/admin/privacy/users/{MEMBER}/erase"


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def settings() -> Settings:
    return make_settings()


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


@pytest.fixture
def admin_cookie(settings: Settings) -> dict[str, str]:
    return make_cookie(settings, ADMIN)


# ── Guard ─────────────────────────────────────────────────────────────────────

ROUTES = [(EXPORT, None), (ERASE, {"confirm": MEMBER})]


@pytest.mark.parametrize("path,body", ROUTES)
def test_admin_routes_require_login(
    client: TestClient, fake: FakePlatform, path: str, body: dict | None,
) -> None:
    resp = client.post(PREFIX + path, json=body)
    assert resp.status_code in (401, 403)
    assert fake.calls == []


@pytest.mark.parametrize("path,body", ROUTES)
def test_admin_routes_require_tenant_admin(
    client: TestClient, db_factory, fake: FakePlatform, admin_cookie: dict[str, str],
    path: str, body: dict | None,
) -> None:
    seed_rows(client, db_factory, MEMBER)
    fake.admin_allowed = False

    resp = client.post(PREFIX + path, json=body, cookies=admin_cookie)

    assert resp.status_code == 403
    [check] = fake.calls
    assert check.service == "authz"
    assert check.json["action"] == "session.delete"
    assert check.json["user_id"] == ADMIN
    assert count_rows(client, db_factory, MEMBER) == SEEDED


@pytest.mark.parametrize("path,body", ROUTES)
def test_admin_guard_fails_closed_when_authz_is_down(
    client: TestClient, db_factory, fake: FakePlatform, admin_cookie: dict[str, str],
    path: str, body: dict | None,
) -> None:
    fake.down.add("authz")
    resp = client.post(PREFIX + path, json=body, cookies=admin_cookie)
    assert resp.status_code == 403
    assert fake.data_calls() == []


def test_guard_runs_before_body_validation(
    client: TestClient, db_factory, fake: FakePlatform, admin_cookie: dict[str, str],
) -> None:
    fake.admin_allowed = False
    resp = client.post(PREFIX + ERASE, json={}, cookies=admin_cookie)
    assert resp.status_code == 403


@pytest.mark.parametrize("bad_id", ["not-a-user", "usr_lowercase0000000000000000", "usr_01J5"])
def test_admin_routes_reject_a_malformed_user_id(
    client: TestClient, db_factory, fake: FakePlatform, admin_cookie: dict[str, str],
    bad_id: str,
) -> None:
    for action, body in (("export", None), ("erase", {"confirm": bad_id})):
        resp = client.post(
            PREFIX + f"/admin/privacy/users/{bad_id}/{action}", json=body, cookies=admin_cookie,
        )
        assert resp.status_code == 422, (action, resp.text)
    assert fake.data_calls() == []


# ── Export ────────────────────────────────────────────────────────────────────

def test_admin_export_returns_only_the_subjects_data(
    client: TestClient, db_factory, fake: FakePlatform, admin_cookie: dict[str, str],
) -> None:
    member_hash = seed_rows(client, db_factory, MEMBER)
    seed_rows(client, db_factory, ADMIN, ack_versions=("admin-only",))
    seed_rows(client, db_factory, USER_B)

    day_before = datetime.now(UTC).strftime("%Y-%m-%d")
    resp = client.post(PREFIX + EXPORT, cookies=admin_cookie)
    days = {day_before, datetime.now(UTC).strftime("%Y-%m-%d")}

    assert resp.status_code == 200, resp.text
    assert resp.headers["content-disposition"] in {
        f'attachment; filename="user-data-{MEMBER}-{day}.json"' for day in days
    }
    body = resp.json()
    assert body["subject_user_id"] == MEMBER
    assert body["platform"]["subject_user_id"] == MEMBER
    assert body["profile"] == {"id": MEMBER, "email": "member@example.com"}
    assert [m["id"] for m in body["memories"]] == ["doc_A1", "doc_A2"]
    assert [j["id"] for j in body["schedules"]] == ["cron_A1", "cron_A2"]
    assert body["preferences"]["llm"] == [
        {"purpose": "llm", "model_id": "model-a", "provider_id": "prov-1"},
    ]
    assert [a["id"] for a in body["apps"]["authored"]] == ["app_A1"]
    assert [k["key_prefix"] for k in body["api_keys"]] == [f"ap_{MEMBER[-6:]}"]
    assert [a["notice_version"] for a in body["privacy_acknowledgements"]] == [
        "2026-09.aaaaaaaaaaaa", "2026-10.bbbbbbbbbbbb",
    ]
    assert member_hash not in resp.text
    assert "admin-only" not in resp.text
    assert USER_B not in resp.text


def test_admin_export_calls_upstreams_as_admin_for_the_subject(
    client: TestClient, db_factory, fake: FakePlatform, admin_cookie: dict[str, str],
) -> None:
    client.post(PREFIX + EXPORT, cookies=admin_cookie)

    [orch] = fake.calls_to("orchestrator")
    assert (orch.method, orch.path, orch.params) == ("GET", "/privacy/export", {"user_id": MEMBER})
    [knowledge] = fake.calls_to("knowledge")
    assert (knowledge.method, knowledge.path) == (
        "GET", f"/internal/privacy/users/{MEMBER}/memories",
    )
    # The gateway's member routes and app-registry name the subject too.
    [llm] = fake.calls_to("llm")
    assert (llm.method, llm.path) == ("GET", f"/users/{MEMBER}/preferences")
    [apps] = fake.calls_to("apps")
    assert (apps.method, apps.path, apps.params) == (
        "GET", "/privacy/apps", {"user_id": MEMBER},
    )
    for call in (orch, knowledge, llm, apps):
        assert call.headers["X-Actor"] == "ADMIN"
        assert call.headers["X-User-Id"] == ADMIN
        assert call.headers["X-Tenant-Id"] == TENANT
    [identity] = fake.calls_to("identity")
    assert identity.path == f"/users/{MEMBER}"
    assert "X-Actor" not in identity.headers
    [cron] = fake.calls_to("cron")
    assert cron.path == f"/internal/privacy/users/{MEMBER}/cron-jobs"
    assert fake.unexpected == []


def test_admin_export_is_audited_with_counts_only(
    client: TestClient, db_factory, fake: FakePlatform, admin_cookie: dict[str, str],
) -> None:
    seed_rows(client, db_factory, MEMBER)

    client.post(PREFIX + EXPORT, cookies=admin_cookie)

    [event] = fake.audit_events
    assert_valid_audit_event(event)
    assert event["event_type"] == "admin.privacy.user.exported"
    assert event["outcome"] == "SUCCESS"
    assert event["actor_kind"] == "ADMIN"
    assert event["actor_user_id"] == ADMIN
    assert (event["resource_type"], event["resource_id"]) == ("user", MEMBER)
    assert event["payload"] == {
        "via": "admin_dsar",
        "counts": {
            "sessions": 1, "workspaces": 1, "memories": 2, "schedules": 2,
            "llm_preferences": 1, "apps": 1, "api_keys": 1,
        },
    }
    assert MEMORY_TEXT not in json.dumps(event)


def test_admin_export_failure_is_502_and_audited(
    client: TestClient, db_factory, fake: FakePlatform, admin_cookie: dict[str, str],
) -> None:
    fake.down.add("knowledge")

    resp = client.post(PREFIX + EXPORT, cookies=admin_cookie)

    assert resp.status_code == 502
    assert resp.json()["detail"]["service"] == "knowledge-ingest"
    [event] = fake.audit_events
    assert_valid_audit_event(event)
    assert event["outcome"] == "FAILURE"
    assert event["payload"] == {"via": "admin_dsar", "failed": "knowledge-ingest"}


# ── Erase ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("confirm", [ADMIN, "DELETE MY DATA", MEMBER.lower(), f" {MEMBER}"])
def test_admin_erase_requires_the_subjects_id_as_confirmation(
    client: TestClient, db_factory, fake: FakePlatform, admin_cookie: dict[str, str],
    confirm: str,
) -> None:
    seed_rows(client, db_factory, MEMBER)

    resp = client.post(PREFIX + ERASE, json={"confirm": confirm}, cookies=admin_cookie)

    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["user_message"] == "Type the user's ID to confirm the erasure."
    assert detail["field"] == "confirm"
    assert fake.data_calls() == []
    assert fake.audit_events == []
    assert count_rows(client, db_factory, MEMBER) == SEEDED


def test_admin_erase_deletes_only_the_subjects_data(
    client: TestClient, db_factory, fake: FakePlatform, admin_cookie: dict[str, str],
) -> None:
    seed_rows(client, db_factory, MEMBER)
    seed_rows(client, db_factory, ADMIN)
    # (notification_preferences is unique on user_id + event_key, across tenants)
    seed_rows(client, db_factory, MEMBER, tenant_id=OTHER_TENANT, event_key="needs_review")

    resp = client.post(PREFIX + ERASE, json={"confirm": MEMBER}, cookies=admin_cookie)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["platform"]["subject_user_id"] == MEMBER
    assert body["memories_deleted"] == 2
    assert body["schedules"] == {"deleted": 2}
    assert body["llm_preferences"] == {"deleted": 1}
    assert body["apps"] == {
        "apps_deleted": 1, "apps_deattributed": 0, "versions_deattributed": 0,
    }
    assert body["bff"] == SEEDED
    assert body["account"] == ADMIN_ACCOUNT_NOTE

    cron_path = f"/internal/privacy/users/{MEMBER}/cron-jobs"
    memories_path = f"/internal/privacy/users/{MEMBER}/memories"
    assert fake.data_calls() == [
        # Preflight: nothing changes.
        ("cron", "GET", cron_path),
        ("orchestrator", "POST", "/privacy/erase"),
        ("knowledge", "GET", memories_path),
        ("llm", "GET", f"/users/{MEMBER}/preferences"),
        ("apps", "POST", "/privacy/apps/erase"),
        # The erasure.
        ("cron", "DELETE", cron_path),
        ("orchestrator", "POST", "/privacy/erase"),
        ("knowledge", "DELETE", memories_path),
        ("llm", "DELETE", f"/users/{MEMBER}/preferences"),
        ("apps", "POST", "/privacy/apps/erase"),
    ]
    assert [c.json for c in fake.calls_to("orchestrator")] == [
        {"dry_run": True, "user_id": MEMBER}, {"dry_run": False, "user_id": MEMBER},
    ]
    assert [c.json for c in fake.calls_to("apps")] == [
        {"dry_run": True, "user_id": MEMBER}, {"dry_run": False, "user_id": MEMBER},
    ]
    assert fake.calls_to("knowledge")[-1].params == {}
    for call in (
        *fake.calls_to("orchestrator"), *fake.calls_to("knowledge"),
        *fake.calls_to("llm"), *fake.calls_to("apps"),
    ):
        assert call.headers["X-Actor"] == "ADMIN"
        assert call.headers["X-User-Id"] == ADMIN

    assert count_rows(client, db_factory, MEMBER) == NO_ROWS
    assert count_rows(client, db_factory, ADMIN) == SEEDED
    # Same user id in another tenant: out of this admin's reach.
    assert count_rows(client, db_factory, MEMBER, tenant_id=OTHER_TENANT) == SEEDED
    assert MEMBER not in fake.memories
    assert USER_B in fake.memories
    assert MEMBER not in fake.schedules
    assert USER_B in fake.schedules
    assert MEMBER not in fake.llm_preferences
    assert USER_B in fake.llm_preferences
    assert MEMBER not in fake.apps
    assert USER_B in fake.apps


def test_admin_erase_is_audited(
    client: TestClient, db_factory, fake: FakePlatform, admin_cookie: dict[str, str],
) -> None:
    client.post(PREFIX + ERASE, json={"confirm": MEMBER}, cookies=admin_cookie)

    [event] = fake.audit_events
    assert_valid_audit_event(event)
    assert event["event_type"] == "admin.privacy.user.erased"
    assert event["outcome"] == "SUCCESS"
    assert event["actor_kind"] == "ADMIN"
    assert event["actor_user_id"] == ADMIN
    assert event["resource_id"] == MEMBER
    assert event["payload"]["via"] == "admin_dsar"
    assert event["payload"]["memories_deleted"] == 2
    assert event["payload"]["schedules_deleted"] == 2
    assert event["payload"]["succeeded"] == [
        "schedules", "platform", "memories", "llm_preferences", "apps", "bff",
    ]
    assert event["payload"]["llm_preferences_deleted"] == 1
    assert "skipped" not in event["payload"]


def test_admin_erase_partial_failure(
    client: TestClient, db_factory, fake: FakePlatform, admin_cookie: dict[str, str],
) -> None:
    seed_rows(client, db_factory, MEMBER)
    fake.write_down.add("knowledge")

    resp = client.post(PREFIX + ERASE, json={"confirm": MEMBER}, cookies=admin_cookie)

    assert resp.status_code == 502
    detail = resp.json()["detail"]
    assert detail["succeeded"] == ["schedules", "platform"]
    assert detail["failed"] == "memories"
    assert detail["not_attempted"] == ["llm_preferences", "apps", "bff"]
    assert "knowledge-ingest unreachable" in detail["error"]
    assert count_rows(client, db_factory, MEMBER) == SEEDED
    [event] = fake.audit_events
    assert event["outcome"] == "PARTIAL"
    assert event["event_type"] == "admin.privacy.user.erased"


@pytest.mark.parametrize("service,step", [
    ("knowledge", "memories"), ("llm", "llm_preferences"), ("apps", "apps"),
])
def test_admin_erase_is_refused_when_a_service_fails_the_preflight(
    client: TestClient, db_factory, fake: FakePlatform, admin_cookie: dict[str, str],
    service: str, step: str,
) -> None:
    seed_rows(client, db_factory, MEMBER)
    fake.status[service] = 500

    resp = client.post(PREFIX + ERASE, json={"confirm": MEMBER}, cookies=admin_cookie)

    assert resp.status_code == 503
    detail = resp.json()["detail"]
    assert detail["code"] == "PRIVACY_ERASURE_UNAVAILABLE"
    assert detail["failed"] == step
    assert fake.writes() == []
    assert count_rows(client, db_factory, MEMBER) == SEEDED
    [event] = fake.audit_events
    assert event["event_type"] == "admin.privacy.user.erased"
    assert event["outcome"] == "FAILURE"
    assert event["payload"] == {"via": "admin_dsar", "phase": "preflight", "failed": step}
