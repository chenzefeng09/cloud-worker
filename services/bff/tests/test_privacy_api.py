"""Tests: GDPR self-service privacy API (``api/v1/privacy.py``).

Covers:
1. Every route requires the session cookie and reaches no upstream without it.
2. Export — every source included (every one required), API-key hashes never
   exported, the download headers.
3. Erasure — typed confirmation, a preflight that touches nothing and refuses
   the erasure (503) when any service fails it, step order (schedules →
   orchestrator → memories → LLM preferences → Apps → BFF rows), other users
   untouched, 502 naming finished steps on a failure after the preflight, safe
   re-run, skipped steps reported as skipped, audit events that pass
   foundation-audit validation.
4. Privacy settings + memories proxies.
5. Privacy notice — shape, content-hash version, env-driven settings,
   retention / memory fail-soft.
6. Notice acknowledgements — append-only, latest, per-user, consent forward.
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_bff.api.v1 import privacy as privacy_module
from agent_bff.config import PrivacyNoticePurpose, Settings
from agent_bff.domain.web_sessions import WebSession
from tests.privacy_fakes import (
    MEMORY_TEXT,
    NO_ROWS,
    PREFIX,
    SEEDED,
    SERVICE_TOKEN,
    TENANT,
    USER_A,
    USER_B,
    WEB_SESSION_TOKEN_ENC,
    FakePlatform,
    app_client,
    assert_valid_audit_event,
    count_rows,
    install_fake,
    make_cookie,
    make_settings,
    run_db,
    seed_rows,
    sqlite_factory,
    web_session_ids,
)

CONFIRM = {"confirm": "DELETE MY DATA"}


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
def cookie_a(settings: Settings) -> dict[str, str]:
    return make_cookie(settings, USER_A)


@pytest.fixture
def cookie_b(settings: Settings) -> dict[str, str]:
    return make_cookie(settings, USER_B)


def _today() -> set[str]:
    return {datetime.now(UTC).strftime("%Y-%m-%d")}


# ── Authentication ────────────────────────────────────────────────────────────

ROUTES: list[tuple[str, str, dict[str, Any] | None]] = [
    ("GET", "/me/privacy/export", None),
    ("POST", "/me/privacy/erase", CONFIRM),
    ("GET", "/me/privacy/settings", None),
    ("PUT", "/me/privacy/settings", {"memory_enabled": False}),
    ("GET", "/me/memories", None),
    ("DELETE", "/me/memories/doc_A1", None),
    ("DELETE", "/me/memories", None),
    ("GET", "/privacy/notice", None),
    ("POST", "/me/privacy/acknowledgements", {"notice_version": "abc"}),
    ("GET", "/me/privacy/acknowledgements/latest", None),
]


@pytest.mark.parametrize("method,path,body", ROUTES)
def test_routes_require_login(
    client: TestClient, fake: FakePlatform, method: str, path: str, body: dict | None,
) -> None:
    resp = client.request(method, PREFIX + path, json=body)
    assert resp.status_code in (401, 403), resp.text
    assert fake.calls == []


# ── Export ────────────────────────────────────────────────────────────────────

def test_export_contains_every_source(
    client: TestClient, db_factory, fake: FakePlatform, cookie_a: dict[str, str],
) -> None:
    hash_a = seed_rows(client, db_factory, USER_A)
    seed_rows(client, db_factory, USER_B)

    # Dates read before and after, so a run straddling midnight UTC still passes.
    before = _today()
    resp = client.get(PREFIX + "/me/privacy/export", cookies=cookie_a)
    days = before | _today()

    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("application/json")
    assert resp.headers["content-disposition"] in {
        f'attachment; filename="my-data-{day}.json"' for day in days
    }
    assert resp.headers["cache-control"] == "no-store"

    body = resp.json()
    assert set(body) == {
        "generated_at", "profile", "platform", "memories", "schedules", "apps",
        "preferences", "api_keys", "privacy_acknowledgements", "sign_in_sessions",
    }
    assert datetime.fromisoformat(body["generated_at"]).tzinfo is not None
    assert body["profile"]["email"] == "ada@example.com"
    assert body["platform"]["subject_user_id"] == USER_A
    assert body["platform"]["sessions"] == [{"id": "ses_A1", "title": "Quarterly report"}]
    assert [m["id"] for m in body["memories"]] == ["doc_A1", "doc_A2"]
    assert [j["id"] for j in body["schedules"]] == ["cron_A1", "cron_A2"]
    assert body["schedules"][0]["input_values"] == {"text": {"topic": "quarterly numbers"}}
    assert body["preferences"]["llm"] == [
        {"purpose": "llm", "model_id": "model-a", "provider_id": "prov-1"},
    ]
    assert [a["id"] for a in body["apps"]["authored"]] == ["app_A1"]
    assert body["apps"]["authored"][0]["versions"][0]["system_prompt"] == "You summarise."
    assert body["apps"]["review_trail"] == []
    [pref] = body["preferences"]["notifications"]
    assert pref["event_key"] == "task_completed"
    assert pref["email_enabled"] is False
    assert pref["push_enabled"] is True

    [key] = body["api_keys"]
    assert set(key) == {"id", "name", "key_prefix", "created_at", "last_used_at", "revoked_at"}
    assert key["name"] == "CI pipeline"
    assert key["key_prefix"] == f"ap_{USER_A[-6:]}"
    assert hash_a not in resp.text
    assert "key_hash" not in resp.text

    versions = [a["notice_version"] for a in body["privacy_acknowledgements"]]
    assert versions == ["2026-09.aaaaaaaaaaaa", "2026-10.bbbbbbbbbbbb"]

    # Sign-ins: when, never the session ids or the stored tokens.
    assert len(body["sign_in_sessions"]) == 2
    for sign_in in body["sign_in_sessions"]:
        assert set(sign_in) == {"created_at", "refreshed_at", "expires_at"}
        assert datetime.fromisoformat(sign_in["expires_at"]) > datetime.fromisoformat(
            sign_in["created_at"]
        )
    for row_id in web_session_ids(USER_A):
        assert row_id not in resp.text
    assert WEB_SESSION_TOKEN_ENC not in resp.text

    # Nothing of user B's.
    assert USER_B not in resp.text
    assert f"ap_{USER_B[-6:]}" not in resp.text


def test_export_calls_upstreams_as_the_caller(
    client: TestClient, db_factory, fake: FakePlatform, cookie_a: dict[str, str],
) -> None:
    client.get(PREFIX + "/me/privacy/export", cookies=cookie_a)

    [orch] = fake.calls_to("orchestrator")
    assert (orch.method, orch.path, orch.params) == ("GET", "/privacy/export", {})
    [knowledge] = fake.calls_to("knowledge")
    assert (knowledge.method, knowledge.path) == ("GET", "/memories")
    [llm] = fake.calls_to("llm")
    assert (llm.method, llm.path) == ("GET", "/user-preferences")
    [apps] = fake.calls_to("apps")
    assert (apps.method, apps.path, apps.params) == ("GET", "/privacy/apps", {})
    for call in (orch, knowledge, llm, apps):
        assert call.headers["X-User-Id"] == USER_A
        assert call.headers["X-Tenant-Id"] == TENANT
        assert call.headers["Authorization"] == "Bearer fake-jwt"
        assert "X-Actor" not in call.headers
    [identity] = fake.calls_to("identity")
    assert identity.path == "/sessions/me"
    # cron-runner's privacy surface is internal: service token, never the user's JWT.
    [cron] = fake.calls_to("cron")
    assert (cron.method, cron.path) == ("GET", f"/internal/privacy/users/{USER_A}/cron-jobs")
    assert cron.headers == {"X-Service-Token": SERVICE_TOKEN, "X-Tenant-Id": TENANT}
    assert fake.unexpected == []


@pytest.mark.parametrize(
    "service,failure,label",
    [
        ("orchestrator", "down", "session-orchestrator"),
        ("orchestrator", 500, "session-orchestrator"),
        ("knowledge", "down", "knowledge-ingest"),
        ("knowledge", 503, "knowledge-ingest"),
        ("cron", "down", "cron-runner"),
        ("cron", 500, "cron-runner"),
        # The profile and the model preferences are part of what is held too:
        # an export without them would misstate it (an expired token, say).
        ("identity", 401, "identity"),
        ("identity", "down", "identity"),
        ("llm", "down", "llm-gateway"),
        ("llm", 500, "llm-gateway"),
        ("apps", "down", "app-registry"),
        ("apps", 403, "app-registry"),
    ],
)
def test_export_fails_when_a_required_source_is_unavailable(
    client: TestClient, db_factory, fake: FakePlatform, cookie_a: dict[str, str],
    service: str, failure: str | int, label: str,
) -> None:
    if failure == "down":
        fake.down.add(service)
    else:
        fake.status[service] = int(failure)

    resp = client.get(PREFIX + "/me/privacy/export", cookies=cookie_a)

    assert resp.status_code == 502, resp.text
    detail = resp.json()["detail"]
    assert detail["service"] == label
    assert detail["code"] == "PRIVACY_EXPORT_UNAVAILABLE"
    assert "export could not be completed" in detail["user_message"]
    assert "content-disposition" not in resp.headers


def test_export_rejects_an_unusable_memory_listing(
    client: TestClient, db_factory, fake: FakePlatform, cookie_a: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = fake._knowledge

    def _broken(call):
        if call.method == "GET":
            return httpx.Response(200, json={"rows": []})
        return original(call)

    monkeypatch.setattr(fake, "_knowledge", _broken)
    resp = client.get(PREFIX + "/me/privacy/export", cookies=cookie_a)
    assert resp.status_code == 502
    assert resp.json()["detail"]["service"] == "knowledge-ingest"


@pytest.mark.parametrize("override", [{"service_token": ""}, {"cron_runner_url": ""}])
def test_export_without_cron_runner_has_no_schedules(
    db_factory, fake: FakePlatform, monkeypatch: pytest.MonkeyPatch, override: dict[str, str],
) -> None:
    settings = make_settings(**override)
    with app_client(settings, monkeypatch) as client:
        resp = client.get(
            PREFIX + "/me/privacy/export", cookies=make_cookie(settings, USER_A),
        )

    assert resp.status_code == 200, resp.text
    assert resp.json()["schedules"] is None
    assert fake.calls_to("cron") == []


# ── Erasure ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "body",
    [
        {"confirm": "delete my data"},
        {"confirm": "DELETE MY DATA "},
        {"confirm": ""},
        {},
        None,
    ],
)
def test_erase_requires_the_exact_confirmation(
    client: TestClient, db_factory, fake: FakePlatform, cookie_a: dict[str, str],
    body: dict | None,
) -> None:
    seed_rows(client, db_factory, USER_A)

    resp = client.post(PREFIX + "/me/privacy/erase", json=body, cookies=cookie_a)

    assert resp.status_code == 422, resp.text
    assert fake.data_calls() == []
    assert fake.audit_events == []
    assert count_rows(client, db_factory, USER_A) == SEEDED


def test_erase_confirmation_error_is_user_readable(
    client: TestClient, db_factory, fake: FakePlatform, cookie_a: dict[str, str],
) -> None:
    resp = client.post(PREFIX + "/me/privacy/erase", json={"confirm": "yes"}, cookies=cookie_a)
    detail = resp.json()["detail"]
    assert detail["user_message"] == 'Type "DELETE MY DATA" to confirm.'
    assert detail["field"] == "confirm"


def test_erase_runs_every_step_in_order(
    client: TestClient, db_factory, fake: FakePlatform, cookie_a: dict[str, str],
) -> None:
    seed_rows(client, db_factory, USER_A)
    seed_rows(client, db_factory, USER_B)

    resp = client.post(PREFIX + "/me/privacy/erase", json=CONFIRM, cookies=cookie_a)

    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "schedules": {"deleted": 2},
        "platform": {
            "subject_user_id": USER_A,
            "dry_run": False,
            "deleted": {"sessions": 2, "messages": 7, "workspaces": 1},
            "runs_cancelled": 1,
        },
        "memories_deleted": 2,
        "llm_preferences": {"deleted": 1},
        "apps": {"apps_deleted": 1, "apps_deattributed": 0, "versions_deattributed": 0},
        "bff": SEEDED,
        "account": "Your sign-in account itself is closed by your administrator.",
    }
    cron_path = f"/internal/privacy/users/{USER_A}/cron-jobs"
    assert fake.data_calls() == [
        # Preflight: every service is asked first, and nothing changes.
        ("cron", "GET", cron_path),
        ("orchestrator", "POST", "/privacy/erase"),
        ("knowledge", "GET", "/memories"),
        ("llm", "GET", "/user-preferences"),
        ("apps", "POST", "/privacy/apps/erase"),
        # The erasure itself, in order.
        ("cron", "DELETE", cron_path),
        ("orchestrator", "POST", "/privacy/erase"),
        ("knowledge", "DELETE", "/memories"),
        ("llm", "GET", "/user-preferences"),
        ("llm", "DELETE", "/user-preferences/llm"),
        ("apps", "POST", "/privacy/apps/erase"),
    ]
    assert [c.json for c in fake.calls_to("orchestrator")] == [
        {"dry_run": True}, {"dry_run": False},
    ]
    assert [c.json for c in fake.calls_to("apps")] == [{"dry_run": True}, {"dry_run": False}]
    knowledge = fake.calls_to("knowledge")[-1]
    assert knowledge.params == {"all": "true"}
    for call in (
        *fake.calls_to("orchestrator"), knowledge, *fake.calls_to("llm"), *fake.calls_to("apps"),
    ):
        assert call.headers["X-User-Id"] == USER_A
        assert "X-Actor" not in call.headers
    for cron in fake.calls_to("cron"):
        assert cron.headers == {"X-Service-Token": SERVICE_TOKEN, "X-Tenant-Id": TENANT}

    assert count_rows(client, db_factory, USER_A) == NO_ROWS
    assert count_rows(client, db_factory, USER_B) == SEEDED
    assert USER_A not in fake.memories
    assert [m["id"] for m in fake.memories[USER_B]] == ["doc_B1"]
    assert USER_A not in fake.schedules
    assert [j["id"] for j in fake.schedules[USER_B]] == ["cron_B1"]
    assert fake.llm_preferences[USER_A] == []
    assert len(fake.llm_preferences[USER_B]) == 1
    assert USER_A not in fake.apps
    assert [a["id"] for a in fake.apps[USER_B]] == ["app_B1"]


def test_erase_keeps_the_sign_in_the_request_came_from(
    client: TestClient,
    db_factory,
    fake: FakePlatform,
    cookie_a: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The account stays open, so the browser showing the result stays signed
    in; the user's other sign-ins end."""
    seed_rows(client, db_factory, USER_A)
    current, other = web_session_ids(USER_A)
    monkeypatch.setattr(privacy_module, "get_web_session_id", lambda: current)

    resp = client.post(PREFIX + "/me/privacy/erase", json=CONFIRM, cookies=cookie_a)

    assert resp.status_code == 200, resp.text
    assert resp.json()["bff"]["web_sessions"] == 1
    assert count_rows(client, db_factory, USER_A) == {**NO_ROWS, "web_sessions": 1}

    async def _ids(session: AsyncSession) -> list[str]:
        return list((await session.execute(select(WebSession.id))).scalars())

    assert run_db(client, db_factory, _ids) == [current]
    assert other != current


def test_erase_emits_an_audit_event_with_counts_only(
    client: TestClient, db_factory, fake: FakePlatform, cookie_a: dict[str, str],
) -> None:
    seed_rows(client, db_factory, USER_A)

    client.post(PREFIX + "/me/privacy/erase", json=CONFIRM, cookies=cookie_a)

    [event] = fake.audit_events
    assert_valid_audit_event(event)
    assert event["event_type"] == "privacy.user.erased"
    assert event["outcome"] == "SUCCESS"
    assert event["severity"] == "NOTICE"
    assert event["actor_kind"] == "USER"
    assert event["actor_user_id"] == USER_A
    assert (event["resource_type"], event["resource_id"]) == ("user", USER_A)
    assert event["tenant_id"] == TENANT
    assert event["payload"] == {
        "via": "self_service",
        # No ledger bucket configured: erased, but a backup restore can't replay it.
        "ledger": "not_configured",
        "succeeded": ["schedules", "platform", "memories", "llm_preferences", "apps", "bff"],
        "schedules_deleted": 2,
        "platform_deleted": {"sessions": 2, "messages": 7, "workspaces": 1},
        "runs_cancelled": 1,
        "memories_deleted": 2,
        "llm_preferences_deleted": 1,
        "apps": {"apps_deleted": 1, "apps_deattributed": 0, "versions_deattributed": 0},
        "bff": SEEDED,
    }
    assert MEMORY_TEXT not in json.dumps(event)


def test_erase_partial_failure_names_finished_steps_and_can_be_retried(
    client: TestClient, db_factory, fake: FakePlatform, cookie_a: dict[str, str],
) -> None:
    seed_rows(client, db_factory, USER_A)
    # knowledge-ingest passes the preflight, then fails the deletion itself.
    fake.write_down.add("knowledge")

    resp = client.post(PREFIX + "/me/privacy/erase", json=CONFIRM, cookies=cookie_a)

    assert resp.status_code == 502, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == "PRIVACY_ERASURE_INCOMPLETE"
    assert "safe to try again" in detail["user_message"]
    assert detail["succeeded"] == ["schedules", "platform"]
    assert detail["skipped"] == {}
    assert detail["failed"] == "memories"
    assert detail["not_attempted"] == ["llm_preferences", "apps", "bff"]
    assert "knowledge-ingest unreachable" in detail["error"]
    assert detail["results"]["platform"]["runs_cancelled"] == 1
    assert count_rows(client, db_factory, USER_A) == SEEDED  # BFF step never ran
    [event] = fake.audit_events
    assert event["outcome"] == "PARTIAL"
    assert event["payload"]["failed"] == "memories"

    # Every step is idempotent: once knowledge-ingest is back, re-running finishes.
    fake.write_down.clear()
    retry = client.post(PREFIX + "/me/privacy/erase", json=CONFIRM, cookies=cookie_a)

    assert retry.status_code == 200, retry.text
    assert retry.json()["memories_deleted"] == 2
    assert retry.json()["schedules"] == {"deleted": 0}  # already gone: idempotent
    assert count_rows(client, db_factory, USER_A) == NO_ROWS
    assert [c.json for c in fake.calls_to("orchestrator")] == [
        {"dry_run": True}, {"dry_run": False}, {"dry_run": True}, {"dry_run": False},
    ]
    assert fake.audit_events[-1]["outcome"] == "SUCCESS"


def test_erase_stops_when_the_platform_step_fails(
    client: TestClient, db_factory, fake: FakePlatform, cookie_a: dict[str, str],
) -> None:
    seed_rows(client, db_factory, USER_A)
    fake.write_down.add("orchestrator")

    resp = client.post(PREFIX + "/me/privacy/erase", json=CONFIRM, cookies=cookie_a)

    assert resp.status_code == 502
    detail = resp.json()["detail"]
    assert detail["succeeded"] == ["schedules"]
    assert detail["failed"] == "platform"
    assert detail["not_attempted"] == ["memories", "llm_preferences", "apps", "bff"]
    assert detail["results"] == {"schedules": {"deleted": 2}}
    assert [c.method for c in fake.calls_to("knowledge")] == ["GET"]  # the preflight only
    assert count_rows(client, db_factory, USER_A) == SEEDED
    assert fake.audit_events[-1]["outcome"] == "PARTIAL"


def test_erase_stops_when_the_schedules_step_fails(
    client: TestClient, db_factory, fake: FakePlatform, cookie_a: dict[str, str],
) -> None:
    seed_rows(client, db_factory, USER_A)
    fake.write_down.add("cron")

    resp = client.post(PREFIX + "/me/privacy/erase", json=CONFIRM, cookies=cookie_a)

    assert resp.status_code == 502
    detail = resp.json()["detail"]
    assert detail["succeeded"] == []
    assert detail["failed"] == "schedules"
    assert detail["not_attempted"] == ["platform", "memories", "llm_preferences", "apps", "bff"]
    assert "cron-runner unreachable" in detail["error"]
    assert [c.json for c in fake.calls_to("orchestrator")] == [{"dry_run": True}]
    assert count_rows(client, db_factory, USER_A) == SEEDED
    assert fake.audit_events[-1]["outcome"] == "FAILURE"


def test_erase_reports_schedules_as_skipped_without_cron_runner(
    db_factory, fake: FakePlatform, monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = make_settings(service_token="")
    with app_client(settings, monkeypatch) as client:
        resp = client.post(
            PREFIX + "/me/privacy/erase", json=CONFIRM, cookies=make_cookie(settings, USER_A),
        )

    assert resp.status_code == 200, resp.text
    assert resp.json()["schedules"] == {"skipped": "not_configured"}
    assert fake.calls_to("cron") == []
    [event] = fake.audit_events
    assert event["payload"]["succeeded"] == [
        "platform", "memories", "llm_preferences", "apps", "bff",
    ]
    assert event["payload"]["skipped"] == {"schedules": "not_configured"}
    assert "schedules_deleted" not in event["payload"]


@pytest.mark.parametrize(
    "listing",
    [{"items": [{"model_id": "no-purpose"}]}, {"items": [{"purpose": "../admin"}]}, {"x": 1}],
)
def test_erase_rejects_an_unusable_llm_preference_listing(
    client: TestClient, db_factory, fake: FakePlatform, cookie_a: dict[str, str],
    monkeypatch: pytest.MonkeyPatch, listing: dict[str, Any],
) -> None:
    monkeypatch.setattr(fake, "_llm", lambda call: httpx.Response(200, json=listing))

    resp = client.post(PREFIX + "/me/privacy/erase", json=CONFIRM, cookies=cookie_a)

    # The preflight catches it, so nothing is erased.
    assert resp.status_code == 503
    detail = resp.json()["detail"]
    assert detail["code"] == "PRIVACY_ERASURE_UNAVAILABLE"
    assert detail["failed"] == "llm_preferences"
    assert fake.writes() == []
    assert all(c.method == "GET" for c in fake.calls_to("llm"))


def test_erase_counts_an_already_deleted_llm_preference_as_done(
    client: TestClient, db_factory, fake: FakePlatform, cookie_a: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _gone(call):
        if call.method == "GET":
            return httpx.Response(200, json={"items": [{"purpose": "llm"}]})
        return httpx.Response(404, json={"detail": "preference not found"})

    monkeypatch.setattr(fake, "_llm", _gone)

    resp = client.post(PREFIX + "/me/privacy/erase", json=CONFIRM, cookies=cookie_a)

    assert resp.status_code == 200, resp.text
    assert resp.json()["llm_preferences"] == {"deleted": 1}


def test_erase_fails_when_the_llm_gateway_rejects_a_delete(
    client: TestClient, db_factory, fake: FakePlatform, cookie_a: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = fake._llm

    def _locked(call):
        if call.method == "DELETE":
            return httpx.Response(500, json={"detail": "boom"})
        return original(call)

    monkeypatch.setattr(fake, "_llm", _locked)

    resp = client.post(PREFIX + "/me/privacy/erase", json=CONFIRM, cookies=cookie_a)

    assert resp.status_code == 502
    detail = resp.json()["detail"]
    assert detail["failed"] == "llm_preferences"
    assert "llm-gateway returned HTTP 500" in detail["error"]


def test_erase_reports_a_failed_bff_step(
    client: TestClient, db_factory, fake: FakePlatform, cookie_a: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _boom(_subject):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(privacy_module, "_erase_bff_rows", _boom)

    resp = client.post(PREFIX + "/me/privacy/erase", json=CONFIRM, cookies=cookie_a)

    assert resp.status_code == 502
    detail = resp.json()["detail"]
    assert detail["succeeded"] == [
        "schedules", "platform", "memories", "llm_preferences", "apps",
    ]
    assert detail["failed"] == "bff"
    assert detail["not_attempted"] == []
    assert detail["error"] == "internal error"  # no internals leak to the client
    assert detail["results"]["memories_deleted"] == 2


def test_erase_rejects_an_unusable_memory_result(
    client: TestClient, db_factory, fake: FakePlatform, cookie_a: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = fake._knowledge

    def _broken(call):
        if call.method == "DELETE":
            return httpx.Response(200, json={"ok": True})
        return original(call)

    monkeypatch.setattr(fake, "_knowledge", _broken)

    resp = client.post(PREFIX + "/me/privacy/erase", json=CONFIRM, cookies=cookie_a)

    assert resp.status_code == 502
    assert resp.json()["detail"]["failed"] == "memories"


@pytest.mark.parametrize(
    "service,failure,step",
    [
        ("cron", "down", "schedules"),
        ("cron", 500, "schedules"),
        ("orchestrator", 500, "platform"),
        # e.g. a role without the permission: refused before anything is erased.
        ("orchestrator", 403, "platform"),
        ("knowledge", "down", "memories"),
        ("llm", "down", "llm_preferences"),
        ("apps", "down", "apps"),
        ("apps", 403, "apps"),
    ],
)
def test_erase_is_refused_when_a_service_fails_the_preflight(
    client: TestClient, db_factory, fake: FakePlatform, cookie_a: dict[str, str],
    service: str, failure: str | int, step: str,
) -> None:
    seed_rows(client, db_factory, USER_A)
    if failure == "down":
        fake.down.add(service)
    else:
        fake.status[service] = int(failure)

    resp = client.post(PREFIX + "/me/privacy/erase", json=CONFIRM, cookies=cookie_a)

    assert resp.status_code == 503, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == "PRIVACY_ERASURE_UNAVAILABLE"
    assert detail["user_message"].startswith("Nothing was erased")
    assert detail["failed"] == step
    # Nothing changed anywhere.
    assert fake.writes() == []
    assert count_rows(client, db_factory, USER_A) == SEEDED
    assert len(fake.memories[USER_A]) == 2
    assert len(fake.schedules[USER_A]) == 2
    [event] = fake.audit_events
    assert_valid_audit_event(event)
    assert event["outcome"] == "FAILURE"
    assert event["payload"] == {"via": "self_service", "phase": "preflight", "failed": step}


def test_erase_is_refused_when_the_bff_rows_cannot_be_read(
    client: TestClient, db_factory, fake: FakePlatform, cookie_a: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _boom(_subject):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(privacy_module, "_bff_rows", _boom)

    resp = client.post(PREFIX + "/me/privacy/erase", json=CONFIRM, cookies=cookie_a)

    assert resp.status_code == 503
    detail = resp.json()["detail"]
    assert detail["failed"] == "bff"
    assert detail["error"] == "internal error"  # no internals leak to the client
    assert fake.writes() == []


def test_erase_succeeds_when_audit_is_down(
    client: TestClient, db_factory, fake: FakePlatform, cookie_a: dict[str, str],
) -> None:
    fake.down.add("audit")
    resp = client.post(PREFIX + "/me/privacy/erase", json=CONFIRM, cookies=cookie_a)
    assert resp.status_code == 200, resp.text


# ── Privacy settings ──────────────────────────────────────────────────────────

def test_get_privacy_settings_proxies_orchestrator(
    client: TestClient, db_factory, fake: FakePlatform, cookie_a: dict[str, str],
) -> None:
    resp = client.get(PREFIX + "/me/privacy/settings", cookies=cookie_a)

    assert resp.status_code == 200, resp.text
    assert resp.json()["memory_enabled"] is True
    [call] = fake.calls_to("orchestrator")
    assert (call.method, call.path) == ("GET", "/privacy/settings")
    assert call.headers["X-User-Id"] == USER_A


def test_put_privacy_settings_forwards_only_the_flag(
    client: TestClient, db_factory, fake: FakePlatform, cookie_a: dict[str, str],
) -> None:
    resp = client.put(
        PREFIX + "/me/privacy/settings",
        json={"memory_enabled": False, "user_id": USER_B},
        cookies=cookie_a,
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["memory_enabled"] is False
    [call] = fake.calls_to("orchestrator")
    assert call.json == {"memory_enabled": False}
    assert call.headers["X-User-Id"] == USER_A


@pytest.mark.parametrize("body", [{}, {"memory_enabled": "no"}, {"memory_enabled": 0}])
def test_put_privacy_settings_validates_the_flag(
    client: TestClient, db_factory, fake: FakePlatform, cookie_a: dict[str, str], body: dict,
) -> None:
    resp = client.put(PREFIX + "/me/privacy/settings", json=body, cookies=cookie_a)
    assert resp.status_code == 422
    assert fake.calls == []


def test_privacy_settings_relays_upstream_errors(
    client: TestClient, db_factory, fake: FakePlatform, cookie_a: dict[str, str],
) -> None:
    fake.status["orchestrator"] = 503
    assert client.get(PREFIX + "/me/privacy/settings", cookies=cookie_a).status_code == 503

    fake.status.clear()
    fake.down.add("orchestrator")
    resp = client.get(PREFIX + "/me/privacy/settings", cookies=cookie_a)
    assert resp.status_code == 502
    assert resp.json()["detail"]["user_message"].startswith("This service is temporarily")


# ── Memories ──────────────────────────────────────────────────────────────────

def test_list_my_memories(
    client: TestClient, db_factory, fake: FakePlatform, cookie_a: dict[str, str],
) -> None:
    resp = client.get(PREFIX + "/me/memories", cookies=cookie_a)

    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert [m["id"] for m in items] == ["doc_A1", "doc_A2"]
    assert set(items[0]) == {"id", "title", "content", "origin_session_id", "created_at"}


def test_delete_one_memory(
    client: TestClient, db_factory, fake: FakePlatform, cookie_a: dict[str, str],
) -> None:
    resp = client.delete(PREFIX + "/me/memories/doc_A1", cookies=cookie_a)

    assert resp.status_code == 204, resp.text
    assert resp.content == b""
    assert [m["id"] for m in fake.memories[USER_A]] == ["doc_A2"]


def test_delete_someone_elses_memory_is_404(
    client: TestClient, db_factory, fake: FakePlatform, cookie_a: dict[str, str],
) -> None:
    resp = client.delete(PREFIX + "/me/memories/doc_B1", cookies=cookie_a)

    assert resp.status_code == 404
    assert [m["id"] for m in fake.memories[USER_B]] == ["doc_B1"]


def test_delete_memory_rejects_a_malformed_id(
    client: TestClient, db_factory, fake: FakePlatform, cookie_a: dict[str, str],
) -> None:
    resp = client.delete(PREFIX + "/me/memories/doc%20A1", cookies=cookie_a)
    assert resp.status_code == 422
    assert fake.calls == []


def test_delete_all_my_memories(
    client: TestClient, db_factory, fake: FakePlatform, cookie_a: dict[str, str],
) -> None:
    resp = client.delete(PREFIX + "/me/memories", cookies=cookie_a)

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"deleted": 2}
    [call] = fake.calls_to("knowledge")
    assert call.params == {"all": "true"}
    assert USER_A not in fake.memories
    assert USER_B in fake.memories


def test_memories_unreachable_is_502(
    client: TestClient, db_factory, fake: FakePlatform, cookie_a: dict[str, str],
) -> None:
    fake.down.add("knowledge")
    resp = client.get(PREFIX + "/me/memories", cookies=cookie_a)
    assert resp.status_code == 502
    assert resp.json()["detail"]["service"] == "knowledge-ingest"


# ── Privacy notice ────────────────────────────────────────────────────────────

def test_notice_shape(
    client: TestClient, db_factory, fake: FakePlatform, cookie_a: dict[str, str],
) -> None:
    resp = client.get(PREFIX + "/privacy/notice", cookies=cookie_a)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == {
        "version", "controller", "dpo_email", "purposes", "retention", "memory",
        "processors", "rights",
    }
    assert re.fullmatch(r"[0-9a-f]{12}", body["version"])
    assert body["controller"] == {"name": "Your organisation", "contact_email": ""}
    assert body["dpo_email"] is None
    assert len(body["purposes"]) == 5
    for purpose in body["purposes"]:
        assert set(purpose) == {"purpose", "legal_basis"}
        assert "Art. 6(1)" in purpose["legal_basis"]
    assert body["retention"] == {"session_retention_days": 90, "trash_grace_days": 30}
    assert body["memory"] == {"enabled": True}
    for processor in body["processors"]:
        assert set(processor) == {"name", "role", "country", "transfer_mechanism"}
    assert body["rights"] and all(isinstance(r, str) and r for r in body["rights"])
    assert any("Art. 17" in r for r in body["rights"])


def test_notice_version_is_stable_and_ignores_the_memory_setting(
    client: TestClient, db_factory, fake: FakePlatform, cookie_a: dict[str, str],
) -> None:
    first = client.get(PREFIX + "/privacy/notice", cookies=cookie_a).json()
    fake.memory_enabled = False
    second = client.get(PREFIX + "/privacy/notice", cookies=cookie_a).json()

    assert second["memory"] == {"enabled": False}
    assert second["version"] == first["version"]


def test_notice_version_changes_with_retention(
    client: TestClient, db_factory, fake: FakePlatform, cookie_a: dict[str, str],
) -> None:
    first = client.get(PREFIX + "/privacy/notice", cookies=cookie_a).json()
    fake.retention = {"session_retention_days": 30, "trash_grace_days": 30}
    second = client.get(PREFIX + "/privacy/notice", cookies=cookie_a).json()

    assert second["retention"]["session_retention_days"] == 30
    assert second["version"] != first["version"]


def test_notice_fails_soft_when_the_orchestrator_is_down(
    client: TestClient, db_factory, fake: FakePlatform, cookie_a: dict[str, str],
) -> None:
    fake.down.add("orchestrator")

    resp = client.get(PREFIX + "/privacy/notice", cookies=cookie_a)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["retention"] == {"session_retention_days": None, "trash_grace_days": None}
    assert body["memory"] == {"enabled": True}  # never under-state collection


def test_notice_ignores_malformed_retention(
    client: TestClient, db_factory, fake: FakePlatform, cookie_a: dict[str, str],
) -> None:
    fake.retention = {"session_retention_days": "90", "trash_grace_days": True}
    body = client.get(PREFIX + "/privacy/notice", cookies=cookie_a).json()
    assert body["retention"] == {"session_retention_days": None, "trash_grace_days": None}


def test_notice_reflects_configured_settings(
    db_factory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = install_fake(monkeypatch, FakePlatform())
    settings = make_settings(
        privacy_notice_version="2026-10",
        privacy_controller_name="Example GmbH",
        privacy_controller_contact_email="privacy@example.com",
        privacy_dpo_email="dpo@example.com",
        privacy_notice_purposes=[
            PrivacyNoticePurpose(purpose="Run the service", legal_basis="Contract"),
        ],
    )
    with app_client(settings, monkeypatch) as client:
        body = client.get(
            PREFIX + "/privacy/notice", cookies=make_cookie(settings, USER_A),
        ).json()

    assert re.fullmatch(r"2026-10\.[0-9a-f]{12}", body["version"])
    assert body["controller"] == {
        "name": "Example GmbH", "contact_email": "privacy@example.com",
    }
    assert body["dpo_email"] == "dpo@example.com"
    assert body["purposes"] == [{"purpose": "Run the service", "legal_basis": "Contract"}]
    assert fake.unexpected == []


def test_notice_version_hashes_content() -> None:
    settings = make_settings()
    content = {"controller": {"name": "A"}, "purposes": [], "rights": ["x"]}

    version = privacy_module.notice_version(settings, content)

    assert version == privacy_module.notice_version(settings, dict(reversed(content.items())))
    assert version != privacy_module.notice_version(settings, {**content, "rights": ["y"]})
    labelled = privacy_module.notice_version(make_settings(privacy_notice_version=" v2 "), content)
    assert labelled == f"v2.{version}"


def test_privacy_settings_parse_from_json_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BFF_PRIVACY_CONTROLLER_NAME", "Example GmbH")
    monkeypatch.setenv("BFF_PRIVACY_DPO_EMAIL", "dpo@example.com")
    monkeypatch.setenv(
        "BFF_PRIVACY_NOTICE_PURPOSES",
        json.dumps([{"purpose": "Run the service", "legal_basis": "Contract"}]),
    )
    monkeypatch.setenv(
        "BFF_PRIVACY_NOTICE_PROCESSORS",
        json.dumps([{
            "name": "Model Co", "role": "LLM inference", "country": "US",
            "transfer_mechanism": "EU-US Data Privacy Framework",
        }]),
    )

    settings = Settings()

    assert settings.privacy_controller_name == "Example GmbH"
    assert settings.privacy_dpo_email == "dpo@example.com"
    assert [p.purpose for p in settings.privacy_notice_purposes] == ["Run the service"]
    [processor] = settings.privacy_notice_processors
    assert processor.transfer_mechanism == "EU-US Data Privacy Framework"


def test_privacy_settings_defaults_are_not_shared() -> None:
    first, second = make_settings(), make_settings()
    first.privacy_notice_purposes.clear()
    assert len(second.privacy_notice_purposes) == 5


# ── Acknowledgements ──────────────────────────────────────────────────────────

def test_latest_acknowledgement_is_empty_by_default(
    client: TestClient, db_factory, fake: FakePlatform, cookie_a: dict[str, str],
) -> None:
    resp = client.get(PREFIX + "/me/privacy/acknowledgements/latest", cookies=cookie_a)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"notice_version": None, "acknowledged_at": None}


def test_acknowledgements_are_append_only_and_latest_wins(
    client: TestClient, db_factory, fake: FakePlatform, cookie_a: dict[str, str],
) -> None:
    for version in ("v1.aaaaaaaaaaaa", "v2.bbbbbbbbbbbb"):
        resp = client.post(
            PREFIX + "/me/privacy/acknowledgements",
            json={"notice_version": version},
            cookies=cookie_a,
        )
        assert resp.status_code == 204, resp.text
        assert resp.content == b""

    latest = client.get(PREFIX + "/me/privacy/acknowledgements/latest", cookies=cookie_a).json()

    assert latest["notice_version"] == "v2.bbbbbbbbbbbb"
    assert datetime.fromisoformat(latest["acknowledged_at"]).tzinfo is not None
    assert count_rows(client, db_factory, USER_A)["privacy_notice_acknowledgements"] == 2


def test_acknowledgements_are_per_user(
    client: TestClient, db_factory, fake: FakePlatform,
    cookie_a: dict[str, str], cookie_b: dict[str, str],
) -> None:
    client.post(
        PREFIX + "/me/privacy/acknowledgements", json={"notice_version": "v1"}, cookies=cookie_a,
    )
    latest_b = client.get(PREFIX + "/me/privacy/acknowledgements/latest", cookies=cookie_b)
    assert latest_b.json() == {"notice_version": None, "acknowledged_at": None}


@pytest.mark.parametrize(
    "body", [{}, {"notice_version": ""}, {"notice_version": "   "}, {"notice_version": "x" * 101}],
)
def test_acknowledgement_validates_the_version(
    client: TestClient, db_factory, fake: FakePlatform, cookie_a: dict[str, str], body: dict,
) -> None:
    resp = client.post(PREFIX + "/me/privacy/acknowledgements", json=body, cookies=cookie_a)
    assert resp.status_code == 422
    assert count_rows(client, db_factory, USER_A)["privacy_notice_acknowledgements"] == 0
    assert fake.consents == []


def test_acknowledgement_is_mirrored_as_consent(
    client: TestClient, db_factory, fake: FakePlatform, cookie_a: dict[str, str],
) -> None:
    client.post(
        PREFIX + "/me/privacy/acknowledgements",
        json={"notice_version": " v1.aaaaaaaaaaaa "},
        cookies=cookie_a,
    )

    [consent] = fake.consents
    assert consent["user_id"] == USER_A
    assert consent["consent_type"] == "privacy_notice"
    assert consent["granted"] is True
    assert "v1.aaaaaaaaaaaa" in consent["purpose"]
    assert consent["headers"]["X-User-Id"] == USER_A
    latest = client.get(PREFIX + "/me/privacy/acknowledgements/latest", cookies=cookie_a).json()
    assert latest["notice_version"] == "v1.aaaaaaaaaaaa"


@pytest.mark.parametrize("failure", ["down", 500])
def test_acknowledgement_survives_a_consent_failure(
    client: TestClient, db_factory, fake: FakePlatform, cookie_a: dict[str, str],
    failure: str | int,
) -> None:
    if failure == "down":
        fake.down.add("data_protection")
    else:
        fake.status["data_protection"] = int(failure)

    resp = client.post(
        PREFIX + "/me/privacy/acknowledgements", json={"notice_version": "v1"}, cookies=cookie_a,
    )

    assert resp.status_code == 204
    assert count_rows(client, db_factory, USER_A)["privacy_notice_acknowledgements"] == 1
