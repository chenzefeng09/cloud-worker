"""Tests: the personal-data breach register (``admin_breaches.py``, GDPR Art. 33/34).

Covers the super_admin gate, the create / list / get / patch round trip on a
real (SQLite) table, the Art. 33(1) timing rules — awareness before
notification, reasons for a late or missing notification — tenant scoping via
``?tenant_id=``, and that audit events carry field names and counts only.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from agent_bff.config import Settings
from agent_bff.domain.db import set_session_factory
from agent_bff.domain.models import Base
from agent_bff.main import create_app
from tests.privacy_fakes import COOKIE_SECRET, OTHER_TENANT, PREFIX, TENANT, USER_A, make_cookie

BREACHES = PREFIX + "/admin/compliance/breaches"
SECRET = "Payroll export for J. Doe"


class FakeFoundation:
    """authz ``/check`` (configurable) + foundation-audit ``/events`` (recorded)."""

    def __init__(self, *, allowed: bool = True) -> None:
        self.allowed = allowed
        self.audits: list[tuple[dict[str, Any], dict[str, str]]] = []

    def route(self, method: str, url: str, **kw: Any) -> httpx.Response:
        if url.endswith("/check"):
            return httpx.Response(200, json={"allowed": self.allowed})
        if method == "POST" and url.endswith("/events"):
            self.audits.append((kw.get("json") or {}, dict(kw.get("headers") or {})))
            return httpx.Response(201, json={"ok": True})
        return httpx.Response(404, json={"detail": "unexpected call"})


@pytest.fixture
def settings() -> Settings:
    return Settings(
        env="test",
        log_level="WARNING",
        cookie_secret=COOKIE_SECRET,
        cookie_secure=False,
        cors_origins="http://localhost",
        otel_enabled=False,
    )


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings), raise_server_exceptions=False) as c:
        yield c


@pytest_asyncio.fixture
async def db(client: TestClient):  # noqa: ARG001 — after the app's own init_db
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    set_session_factory(async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False))
    yield
    set_session_factory(None)
    await engine.dispose()


def _install(monkeypatch: pytest.MonkeyPatch, fake: FakeFoundation) -> FakeFoundation:
    class _AC:
        def __init__(self, *_a: Any, **_kw: Any) -> None:
            pass

        async def __aenter__(self) -> _AC:
            return self

        async def __aexit__(self, *_exc: Any) -> None:
            return None

        async def post(self, url: str, **kw: Any) -> httpx.Response:
            return fake.route("POST", url, **kw)

        async def get(self, url: str, **kw: Any) -> httpx.Response:
            return fake.route("GET", url, **kw)

    monkeypatch.setattr(httpx, "AsyncClient", _AC)
    return fake


@pytest.fixture
def foundation(monkeypatch: pytest.MonkeyPatch) -> FakeFoundation:
    return _install(monkeypatch, FakeFoundation())


@pytest.fixture
def cookie(settings: Settings) -> dict[str, str]:
    return make_cookie(settings, USER_A)


def _ago(**delta: float) -> str:
    return (datetime.now(UTC) - timedelta(**delta)).isoformat()


def _breach(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "title": "Misdirected payroll email",
        "description": SECRET,
        "aware_at": _ago(hours=5),
        "occurred_at": _ago(hours=30),
        "data_categories": ["contact", "financial"],
        "approx_subjects": 12,
        "approx_records": 40,
        "likely_consequences": "Exposure of salary details",
        "measures_taken": "Recall requested; recipient confirmed deletion",
    }
    body.update(overrides)
    return body


def _create(client: TestClient, cookie: dict[str, str], **overrides: Any) -> dict[str, Any]:
    resp = client.post(BREACHES, json=_breach(**overrides), cookies=cookie)
    assert resp.status_code == 201, resp.text
    return resp.json()


# ── access ───────────────────────────────────────────────────────────────────

def test_register_requires_auth(client: TestClient) -> None:
    assert client.get(BREACHES).status_code == 401


def test_register_requires_super_admin(
    client: TestClient, db: None, cookie: dict[str, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _install(monkeypatch, FakeFoundation(allowed=False))
    assert client.get(BREACHES, cookies=cookie).status_code == 403
    assert client.post(BREACHES, json=_breach(), cookies=cookie).status_code == 403
    assert not fake.audits


# ── round trip ───────────────────────────────────────────────────────────────

def test_create_list_get_round_trip(
    client: TestClient, db: None, cookie: dict[str, str], foundation: FakeFoundation,
) -> None:
    aware = datetime.now(UTC) - timedelta(hours=5)
    created = _create(client, cookie, aware_at=aware.isoformat())
    assert created["id"].startswith("brc_")
    assert created["tenant_id"] == TENANT
    assert created["status"] == "open"
    assert created["risk_level"] == "medium"
    assert created["created_by"] == USER_A
    assert created["data_categories"] == ["contact", "financial"]
    notify_by = datetime.fromisoformat(created["notify_authority_by"])
    assert notify_by == aware + timedelta(hours=72)

    listed = client.get(BREACHES, cookies=cookie).json()["items"]
    assert [b["id"] for b in listed] == [created["id"]]

    fetched = client.get(f"{BREACHES}/{created['id']}", cookies=cookie)
    assert fetched.status_code == 200
    assert fetched.json()["title"] == "Misdirected payroll email"


def test_list_is_newest_awareness_first(
    client: TestClient, db: None, cookie: dict[str, str], foundation: FakeFoundation,
) -> None:
    older = _create(client, cookie, title="older", aware_at=_ago(days=3), occurred_at=None)
    newer = _create(client, cookie, title="newer", aware_at=_ago(hours=1), occurred_at=None)
    listed = client.get(BREACHES, cookies=cookie).json()["items"]
    assert [b["id"] for b in listed] == [newer["id"], older["id"]]


def test_get_unknown_breach_is_404(
    client: TestClient, db: None, cookie: dict[str, str], foundation: FakeFoundation,
) -> None:
    resp = client.get(f"{BREACHES}/brc_missing", cookies=cookie)
    assert resp.status_code == 404


def test_create_is_audited_without_free_text(
    client: TestClient, db: None, cookie: dict[str, str], foundation: FakeFoundation,
) -> None:
    created = _create(client, cookie)
    assert len(foundation.audits) == 1
    event, _headers = foundation.audits[0]
    assert event["event_type"] == "admin.compliance.breach.created"
    assert event["resource_type"] == "breach_record"
    assert event["resource_id"] == created["id"]
    assert event["actor_kind"] == "ADMIN"
    assert event["payload"] == {
        "status": "open",
        "risk_level": "medium",
        "approx_subjects": 12,
        "approx_records": 40,
        "authority_notified": False,
    }
    assert SECRET not in str(event)


# ── validation ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "overrides,field",
    [
        ({"title": "   "}, "title"),
        ({"aware_at": None}, "aware_at"),
        ({"aware_at": "2026-10-01T10:00:00"}, "aware_at"),  # no timezone
        ({"status": "resolved"}, "status"),
        ({"risk_level": "critical"}, "risk_level"),
        ({"approx_subjects": -1}, "approx_subjects"),
        ({"surprise": True}, "surprise"),
    ],
)
def test_create_rejects_bad_fields(
    client: TestClient, db: None, cookie: dict[str, str], foundation: FakeFoundation,
    overrides: dict[str, Any], field: str,
) -> None:
    resp = client.post(BREACHES, json=_breach(**overrides), cookies=cookie)
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["field"] == field
    assert not foundation.audits


def test_awareness_cannot_be_in_the_future(
    client: TestClient, db: None, cookie: dict[str, str], foundation: FakeFoundation,
) -> None:
    future = (datetime.now(UTC) + timedelta(hours=2)).isoformat()
    resp = client.post(BREACHES, json=_breach(aware_at=future), cookies=cookie)
    assert resp.status_code == 422
    assert resp.json()["detail"]["field"] == "aware_at"


def test_breach_cannot_occur_after_awareness(
    client: TestClient, db: None, cookie: dict[str, str], foundation: FakeFoundation,
) -> None:
    resp = client.post(
        BREACHES, json=_breach(aware_at=_ago(hours=5), occurred_at=_ago(hours=1)), cookies=cookie,
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["field"] == "occurred_at"


def test_notification_cannot_precede_awareness(
    client: TestClient, db: None, cookie: dict[str, str], foundation: FakeFoundation,
) -> None:
    resp = client.post(
        BREACHES,
        json=_breach(aware_at=_ago(hours=5), authority_notified_at=_ago(hours=6)),
        cookies=cookie,
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["field"] == "authority_notified_at"


def test_late_notification_needs_reasons(
    client: TestClient, db: None, cookie: dict[str, str], foundation: FakeFoundation,
) -> None:
    late = _breach(aware_at=_ago(days=5), occurred_at=None, authority_notified_at=_ago(days=1))
    resp = client.post(BREACHES, json=late, cookies=cookie)
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["field"] == "delay_reason"
    assert "72 hours" in detail["user_message"]

    late["delay_reason"] = "Scope was only established after the forensic review"
    assert client.post(BREACHES, json=late, cookies=cookie).status_code == 201


def test_notification_inside_72_hours_needs_no_reason(
    client: TestClient, db: None, cookie: dict[str, str], foundation: FakeFoundation,
) -> None:
    on_time = _breach(
        aware_at=_ago(hours=70), occurred_at=None, authority_notified_at=_ago(hours=1),
    )
    assert client.post(BREACHES, json=on_time, cookies=cookie).status_code == 201


# ── updates ──────────────────────────────────────────────────────────────────

def test_closing_without_notifying_needs_reasons_unless_low_risk(
    client: TestClient, db: None, cookie: dict[str, str], foundation: FakeFoundation,
) -> None:
    breach = _create(client, cookie)
    url = f"{BREACHES}/{breach['id']}"

    resp = client.patch(url, json={"status": "closed"}, cookies=cookie)
    assert resp.status_code == 422
    assert resp.json()["detail"]["field"] == "delay_reason"

    # Unlikely to result in a risk → no notification was due.
    resp = client.patch(url, json={"status": "closed", "risk_level": "low"}, cookies=cookie)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "closed"


def test_closing_unnotified_high_risk_breach_with_reasons(
    client: TestClient, db: None, cookie: dict[str, str], foundation: FakeFoundation,
) -> None:
    breach = _create(client, cookie, risk_level="high")
    resp = client.patch(
        f"{BREACHES}/{breach['id']}",
        json={"status": "closed", "delay_reason": "Data was encrypted; key not compromised"},
        cookies=cookie,
    )
    assert resp.status_code == 200, resp.text


def test_patch_cannot_clear_a_required_field(
    client: TestClient, db: None, cookie: dict[str, str], foundation: FakeFoundation,
) -> None:
    breach = _create(client, cookie)
    resp = client.patch(f"{BREACHES}/{breach['id']}", json={"title": None}, cookies=cookie)
    assert resp.status_code == 422
    assert "title" in resp.json()["detail"]["user_message"]


def test_patch_records_notification_and_clears_optional_fields(
    client: TestClient, db: None, cookie: dict[str, str], foundation: FakeFoundation,
) -> None:
    breach = _create(client, cookie, security_incident_id="inc_42")
    url = f"{BREACHES}/{breach['id']}"
    notified = _ago(hours=1)
    resp = client.patch(
        url,
        json={
            "authority_notified_at": notified,
            "authority_reference": "DPA-2026-118",
            "security_incident_id": "",
        },
        cookies=cookie,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert datetime.fromisoformat(body["authority_notified_at"]) == datetime.fromisoformat(notified)
    assert body["authority_reference"] == "DPA-2026-118"
    assert body["security_incident_id"] is None
    # The change survives a fresh read.
    assert client.get(url, cookies=cookie).json()["authority_reference"] == "DPA-2026-118"


def test_patch_audits_the_changed_fields_only(
    client: TestClient, db: None, cookie: dict[str, str], foundation: FakeFoundation,
) -> None:
    breach = _create(client, cookie)
    url = f"{BREACHES}/{breach['id']}"
    foundation.audits.clear()

    unchanged = client.patch(url, json={"title": breach["title"]}, cookies=cookie)
    assert unchanged.status_code == 200
    assert not foundation.audits

    resp = client.patch(
        url, json={"status": "contained", "measures_taken": SECRET + " — revoked"}, cookies=cookie,
    )
    assert resp.status_code == 200
    assert len(foundation.audits) == 1
    event, _headers = foundation.audits[0]
    assert event["event_type"] == "admin.compliance.breach.updated"
    assert event["payload"] == {
        "changed_fields": ["measures_taken", "status"],
        "status": "contained",
        "risk_level": "medium",
    }
    assert SECRET not in str(event)


def test_patch_unknown_breach_is_404(
    client: TestClient, db: None, cookie: dict[str, str], foundation: FakeFoundation,
) -> None:
    resp = client.patch(f"{BREACHES}/brc_missing", json={"status": "closed"}, cookies=cookie)
    assert resp.status_code == 404


# ── tenant scoping ───────────────────────────────────────────────────────────

def test_tenant_override_keeps_registers_apart(
    client: TestClient, db: None, cookie: dict[str, str], foundation: FakeFoundation,
) -> None:
    other = client.post(
        f"{BREACHES}?tenant_id={OTHER_TENANT}", json=_breach(), cookies=cookie,
    )
    assert other.status_code == 201
    other_id = other.json()["id"]
    assert other.json()["tenant_id"] == OTHER_TENANT

    assert client.get(BREACHES, cookies=cookie).json()["items"] == []
    assert client.get(f"{BREACHES}/{other_id}", cookies=cookie).status_code == 404
    scoped = client.get(f"{BREACHES}?tenant_id={OTHER_TENANT}", cookies=cookie).json()["items"]
    assert [b["id"] for b in scoped] == [other_id]

    event, headers = foundation.audits[0]
    assert event["tenant_id"] == OTHER_TENANT
    assert headers["X-Tenant-Id"] == OTHER_TENANT
