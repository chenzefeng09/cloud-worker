"""Shared test doubles for the privacy API tests (self-service + admin DSAR).

``FakePlatform`` stands in for every upstream the privacy routes talk to —
authz, session-orchestrator, knowledge-ingest, cron-runner, identity,
llm-gateway, data-protection and audit — with just enough state to check that an export
reads, and an erasure deletes, the right person's data.  Every call is
recorded; ``down`` / ``status`` inject failures per service.

``FakeS3`` stands in for the erasure-ledger bucket (Object Lock: a stored key
can't be rewritten); ``install_ledger`` points the ledger at it.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import re
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from agent_bff.config import Settings
from agent_bff.domain import db as db_module
from agent_bff.domain import erasure_ledger
from agent_bff.domain.erasure_ledger import LedgerEntry
from agent_bff.domain.models import (
    ApiKey,
    Base,
    NotificationPreference,
    PrivacyNoticeAcknowledgement,
)
from agent_bff.main import create_app

PREFIX = "/api/bff/v1"
COOKIE_SECRET = "abcdefghijklmnopqrstuvwxyz0123456789ABCDEFGHIJKL"

TENANT = "tnt_01J5TEST00000000000000TNT1"
OTHER_TENANT = "tnt_01J5TEST00000000000000TNT2"
USER_A = "usr_01J5TEST0000000000000USER1"
USER_B = "usr_01J5TEST0000000000000USER2"
ADMIN = "usr_01J5TEST000000000000ADMIN1"

AUTHZ_URL = "http://authz-test/api/authz/v1"
ORCH_URL = "http://orch-test:8006/api/session/v1"
KNOWLEDGE_URL = "http://knowledge-test/api/knowledge/v1"
IDENTITY_URL = "http://identity-test/api/identity/v1"
LLM_URL = "http://llm-test/api/llm-gateway/v1"
CRON_URL = "http://cron-test:8013/api/cron/v1"
SERVICE_TOKEN = "test-service-token"
DP_URL = "http://dp-test/api/data-protection/v1"
AUDIT_URL = "http://audit-test/api/audit/v1"

SERVICE_URLS: dict[str, str] = {
    "authz": AUTHZ_URL,
    "orchestrator": ORCH_URL,
    "knowledge": KNOWLEDGE_URL,
    "cron": CRON_URL,
    "identity": IDENTITY_URL,
    "llm": LLM_URL,
    "data_protection": DP_URL,
    "audit": AUDIT_URL,
}

# foundation-audit ``AuditEvent`` is ``extra="forbid"``: an event carrying any
# other key is rejected, so the tests pin the emitted keys to this set.
AUDIT_EVENT_FIELDS = frozenset({
    "event_id", "schema_version", "occurred_at", "tenant_id", "service",
    "service_version", "event_type", "severity", "outcome", "actor_kind",
    "actor_user_id", "actor_api_key_id", "resource_type", "resource_id",
    "resource_path", "request_id", "trace_id", "span_id", "session_id",
    "parent_event_id", "ip", "user_agent", "country", "payload", "sensitive_fields",
})

MEMORY_TEXT = "User prefers quarterly reports as PDF"


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "env": "test",
        "log_level": "WARNING",
        "cookie_secret": COOKIE_SECRET,
        "cookie_secure": False,
        "db_dsn": "sqlite+aiosqlite:///:memory:",
        "authz_url": AUTHZ_URL,
        "foundation_identity_url": IDENTITY_URL,
        "session_orchestrator_url": ORCH_URL,
        "knowledge_ingest_url": KNOWLEDGE_URL,
        "foundation_llm_gateway_url": LLM_URL,
        "cron_runner_url": CRON_URL,
        "service_token": SERVICE_TOKEN,
        "foundation_data_protection_url": DP_URL,
        "foundation_audit_url": AUDIT_URL,
        "cors_origins": "http://localhost",
        "otel_enabled": False,
    }
    values.update(overrides)
    return Settings(**values)


def make_cookie(settings: Settings, user_id: str, tenant_id: str = TENANT) -> dict[str, str]:
    from foundation_sdk import CookieSigner, CookieSignerSettings

    signer = CookieSigner(
        CookieSignerSettings(
            secret_key=settings.cookie_secret,
            salt="agent-bff-session",
            max_age_seconds=settings.cookie_max_age,
        )
    )
    token = signer.sign({"user_id": user_id, "tenant_id": tenant_id, "access_token": "fake-jwt"})
    return {settings.cookie_name: token}


# ─────────────────────────────────────────────────────────────────────────────
# Upstream fake
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Call:
    service: str
    method: str
    path: str
    headers: dict[str, str]
    params: dict[str, Any]
    json: Any


def _schedule(job_id: str, name: str, owner: str) -> dict[str, Any]:
    return {
        "id": job_id,
        "tenant_id": TENANT,
        "name": name,
        "created_by_user_id": owner,
        "input_values": {"text": {"topic": "quarterly numbers"}},
        "has_webhook_secret": False,
    }


def _memory(memory_id: str, title: str, session_id: str) -> dict[str, Any]:
    return {
        "id": memory_id,
        "title": title,
        "content": MEMORY_TEXT,
        "origin_session_id": session_id,
        "created_at": "2026-10-01T09:00:00+00:00",
    }


@dataclass
class FakePlatform:
    admin_allowed: bool = True
    memory_enabled: bool = True
    retention: dict[str, Any] = field(
        default_factory=lambda: {"session_retention_days": 90, "trash_grace_days": 30},
    )
    down: set[str] = field(default_factory=set)
    status: dict[str, int] = field(default_factory=dict)
    memories: dict[str, list[dict[str, Any]]] = field(
        default_factory=lambda: {
            USER_A: [
                _memory("doc_A1", "[memory] Quarterly report", "ses_A1"),
                _memory("doc_A2", "[memory] Travel plans", "ses_A2"),
            ],
            USER_B: [_memory("doc_B1", "[memory] Budget review", "ses_B1")],
        },
    )
    schedules: dict[str, list[dict[str, Any]]] = field(
        default_factory=lambda: {
            USER_A: [
                _schedule("cron_A1", "Weekly digest", USER_A),
                _schedule("cron_A2", "Inbox triage", USER_A),
            ],
            USER_B: [_schedule("cron_B1", "Standup notes", USER_B)],
        },
    )
    llm_preferences: dict[str, list[dict[str, Any]]] = field(
        default_factory=lambda: {
            USER_A: [{"purpose": "llm", "model_id": "model-a", "provider_id": "prov-1"}],
            USER_B: [{"purpose": "embedding", "model_id": "embed-b", "provider_id": "prov-1"}],
        },
    )
    calls: list[Call] = field(default_factory=list)
    audit_events: list[dict[str, Any]] = field(default_factory=list)
    consents: list[dict[str, Any]] = field(default_factory=list)
    unexpected: list[tuple[str, str]] = field(default_factory=list)

    # -- helpers for assertions -------------------------------------------------

    def calls_to(self, service: str) -> list[Call]:
        return [c for c in self.calls if c.service == service]

    def data_calls(self) -> list[tuple[str, str, str]]:
        """(service, method, path) of every call except authz + audit."""
        return [
            (c.service, c.method, c.path)
            for c in self.calls
            if c.service not in {"authz", "audit"}
        ]

    # -- dispatch ---------------------------------------------------------------

    def route(self, method: str, url: str, **kw: Any) -> httpx.Response:
        service = next((s for s, base in SERVICE_URLS.items() if url.startswith(base)), None)
        if service is None:
            self.unexpected.append((method, url))
            return httpx.Response(404, json={"detail": "no fake for this host"})
        call = Call(
            service=service,
            method=method,
            path=url[len(SERVICE_URLS[service]):],
            headers=dict(kw.get("headers") or {}),
            params=dict(kw.get("params") or {}),
            json=kw.get("json"),
        )
        self.calls.append(call)
        if service in self.down:
            raise httpx.ConnectError(f"{service} is down")
        if service in self.status:
            code = self.status[service]
            return httpx.Response(code, json={"title": f"{service} error", "status": code})
        response: httpx.Response | None = getattr(self, f"_{service}")(call)
        if response is None:
            self.unexpected.append((method, url))
            return httpx.Response(404, json={"detail": "no fake for this route"})
        return response

    @staticmethod
    def _subject(call: Call, explicit: str | None) -> str:
        if call.headers.get("X-Actor") == "ADMIN" and explicit:
            return explicit
        return call.headers["X-User-Id"]

    def _authz(self, call: Call) -> httpx.Response | None:
        if call.method == "POST" and call.path == "/check":
            return httpx.Response(200, json={"allowed": self.admin_allowed})
        return None

    def _orchestrator(self, call: Call) -> httpx.Response | None:
        if (call.method, call.path) == ("GET", "/privacy/export"):
            subject = self._subject(call, call.params.get("user_id"))
            return httpx.Response(200, json={
                "subject_user_id": subject,
                "generated_at": "2026-10-30T08:00:00+00:00",
                "sessions": [{"id": "ses_A1", "title": "Quarterly report"}],
                "workspaces": [{"id": "wsp_1", "name": "Research"}],
                "privacy_settings": {"memory_enabled": self.memory_enabled},
            })
        if (call.method, call.path) == ("POST", "/privacy/erase"):
            body = call.json or {}
            return httpx.Response(200, json={
                "subject_user_id": self._subject(call, body.get("user_id")),
                "dry_run": bool(body.get("dry_run", False)),
                "deleted": {"sessions": 2, "messages": 7, "workspaces": 1},
                "runs_cancelled": 1,
            })
        if call.path == "/privacy/settings" and call.method in {"GET", "PUT"}:
            if call.method == "PUT":
                self.memory_enabled = call.json["memory_enabled"]
            return httpx.Response(200, json={
                "memory_enabled": self.memory_enabled,
                "updated_at": "2026-10-30T08:00:00+00:00",
            })
        if (call.method, call.path) == ("GET", "/privacy/retention"):
            return httpx.Response(200, json=self.retention)
        return None

    def _knowledge(self, call: Call) -> httpx.Response | None:
        caller = call.headers.get("X-User-Id", "")
        if call.path == "/memories":
            if call.method == "GET":
                return httpx.Response(200, json={"items": list(self.memories.get(caller, []))})
            if call.method == "DELETE" and call.params.get("all") == "true":
                return httpx.Response(200, json={"deleted": len(self.memories.pop(caller, []))})
            return None
        one = re.fullmatch(r"/memories/([^/]+)", call.path)
        if one and call.method == "DELETE":
            items = self.memories.get(caller, [])
            for item in items:
                if item["id"] == one[1]:
                    items.remove(item)
                    return httpx.Response(204)
            return httpx.Response(404, json={"title": "Not Found", "detail": "Document not found"})
        internal = re.fullmatch(r"/internal/privacy/users/([^/]+)/memories", call.path)
        if internal:
            if call.headers.get("X-Actor") != "ADMIN":
                return httpx.Response(403, json={"detail": "ADMIN actor required"})
            subject = internal[1]
            if call.method == "GET":
                return httpx.Response(200, json={"items": list(self.memories.get(subject, []))})
            if call.method == "DELETE":
                return httpx.Response(200, json={"deleted": len(self.memories.pop(subject, []))})
        return None

    def _cron(self, call: Call) -> httpx.Response | None:
        match = re.fullmatch(r"/internal/privacy/users/([^/]+)/cron-jobs", call.path)
        if not match:
            return None
        if call.headers.get("X-Service-Token") != SERVICE_TOKEN or not call.headers.get(
            "X-Tenant-Id"
        ):
            return httpx.Response(401, json={"detail": "invalid or missing service token"})
        subject = match[1]
        if call.method == "GET":
            return httpx.Response(200, json={"items": list(self.schedules.get(subject, []))})
        if call.method == "DELETE":
            return httpx.Response(200, json={"deleted": len(self.schedules.pop(subject, []))})
        return None

    def _identity(self, call: Call) -> httpx.Response | None:
        if (call.method, call.path) == ("GET", "/sessions/me"):
            return httpx.Response(200, json={
                "user_id": call.headers["X-User-Id"],
                "tenant_id": call.headers["X-Tenant-Id"],
                "email": "ada@example.com",
                "display_name": "Ada",
            })
        user = re.fullmatch(r"/users/([^/]+)", call.path)
        if user and call.method == "GET":
            return httpx.Response(200, json={"id": user[1], "email": "member@example.com"})
        return None

    def _llm(self, call: Call) -> httpx.Response | None:
        caller = call.headers.get("X-User-Id", "")
        if (call.method, call.path) == ("GET", "/user-preferences"):
            return httpx.Response(200, json={"items": list(self.llm_preferences.get(caller, []))})
        one = re.fullmatch(r"/user-preferences/([^/]+)", call.path)
        if one and call.method == "DELETE":
            prefs = self.llm_preferences.get(caller, [])
            for pref in prefs:
                if pref["purpose"] == one[1]:
                    prefs.remove(pref)
                    return httpx.Response(204)
            return httpx.Response(404, json={"detail": "preference not found"})
        return None

    def _data_protection(self, call: Call) -> httpx.Response | None:
        if (call.method, call.path) == ("POST", "/consent"):
            self.consents.append({"headers": call.headers, **call.json})
            return httpx.Response(201, json={"id": "cns_1", **call.json})
        return None

    def _audit(self, call: Call) -> httpx.Response | None:
        if (call.method, call.path) == ("POST", "/events"):
            self.audit_events.append(call.json)
            return httpx.Response(202, json={"accepted": True})
        return None


def install_fake(monkeypatch: pytest.MonkeyPatch, fake: FakePlatform) -> FakePlatform:
    """Route every ``httpx.AsyncClient`` call through ``fake``."""

    class _FakeAsyncClient:
        def __init__(self, *_a: Any, **_kw: Any) -> None:
            pass

        async def __aenter__(self) -> _FakeAsyncClient:
            return self

        async def __aexit__(self, *_exc: Any) -> None:
            return None

        async def get(self, url: str, **kw: Any) -> httpx.Response:
            return fake.route("GET", url, **kw)

        async def post(self, url: str, **kw: Any) -> httpx.Response:
            return fake.route("POST", url, **kw)

        async def put(self, url: str, **kw: Any) -> httpx.Response:
            return fake.route("PUT", url, **kw)

        async def patch(self, url: str, **kw: Any) -> httpx.Response:
            return fake.route("PATCH", url, **kw)

        async def delete(self, url: str, **kw: Any) -> httpx.Response:
            return fake.route("DELETE", url, **kw)

    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)
    return fake


# ─────────────────────────────────────────────────────────────────────────────
# Erasure-ledger bucket fake
# ─────────────────────────────────────────────────────────────────────────────

LEDGER_BUCKET = "privacy-ledger-test"


class FakeS3:
    """In-memory ledger bucket with Object Lock: a stored key can't be rewritten.

    ``fail`` makes every call raise it; ``on_put`` runs before each write, so a
    test can look at what had already happened when the entry was written.
    """

    def __init__(self, page_size: int = 1000) -> None:
        self.objects: dict[str, bytes] = {}
        self.puts: list[dict[str, Any]] = []
        self.page_size = page_size
        self.fail: Exception | None = None
        self.on_put: Callable[[], None] | None = None

    def _check(self) -> None:
        if self.fail is not None:
            raise self.fail

    def put_object(self, **kw: Any) -> dict[str, Any]:
        self._check()
        if self.on_put is not None:
            self.on_put()
        assert kw["Bucket"] == LEDGER_BUCKET
        body = kw["Body"]
        assert kw["ContentMD5"] == base64.b64encode(hashlib.md5(body).digest()).decode()
        if kw["Key"] in self.objects:
            raise RuntimeError("AccessDenied: the object is WORM protected")
        self.objects[kw["Key"]] = body
        self.puts.append(kw)
        return {}

    def list_objects_v2(self, **kw: Any) -> dict[str, Any]:
        self._check()
        assert kw["Bucket"] == LEDGER_BUCKET
        keys = sorted(
            key for key in self.objects
            if key.startswith(kw.get("Prefix", "")) and key > kw.get("StartAfter", "")
        )
        start = int(kw.get("ContinuationToken", "0"))
        page = keys[start:start + self.page_size]
        truncated = start + self.page_size < len(keys)
        response: dict[str, Any] = {"IsTruncated": truncated}
        if page:  # S3 leaves "Contents" out of an empty page
            response["Contents"] = [{"Key": key} for key in page]
        if truncated:
            response["NextContinuationToken"] = str(start + self.page_size)
        return response

    def get_object(self, **kw: Any) -> dict[str, Any]:
        self._check()
        return {"Body": io.BytesIO(self.objects[kw["Key"]])}

    def entries(self) -> list[dict[str, Any]]:
        """Stored entries, oldest first."""
        return [json.loads(self.objects[key]) for key in sorted(self.objects)]


def install_ledger(monkeypatch: pytest.MonkeyPatch, s3: FakeS3) -> FakeS3:
    """Make every ``ErasureLedger`` talk to ``s3`` instead of a real bucket."""
    monkeypatch.setattr(erasure_ledger, "_s3_client", lambda **_kw: s3)
    return s3


def add_ledger_entry(
    s3: FakeS3,
    *,
    at: datetime,
    tenant_id: str,
    user_id: str,
    via: str = "self_service",
    actor_user_id: str | None = None,
) -> LedgerEntry:
    """Store an entry as if it had been recorded at ``at``."""
    entry = LedgerEntry(
        entry_id=uuid.uuid4().hex,
        requested_at=at,
        tenant_id=tenant_id,
        user_id=user_id,
        via=via,
        actor_user_id=actor_user_id or user_id,
    )
    s3.objects[entry.key] = entry.to_json()
    return entry


def assert_valid_audit_event(event: dict[str, Any]) -> None:
    """The event would pass foundation-audit's ``AuditEvent`` validation."""
    assert set(event) <= AUDIT_EVENT_FIELDS, set(event) - AUDIT_EVENT_FIELDS
    assert re.fullmatch(r"evt_[0-9A-Z]{26}", event["event_id"])
    assert event["severity"] in {"DEBUG", "INFO", "NOTICE", "WARN", "ERROR", "SECURITY", "CRITICAL"}
    assert event["outcome"] in {"SUCCESS", "FAILURE", "DENIED", "PARTIAL"}
    assert event["actor_kind"] in {"USER", "ADMIN"}
    assert datetime.fromisoformat(event["occurred_at"]).tzinfo is not None
    assert isinstance(event["payload"], dict)


# ─────────────────────────────────────────────────────────────────────────────
# App + DB
# ─────────────────────────────────────────────────────────────────────────────

@asynccontextmanager
async def sqlite_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Fresh in-memory SQLite DB wired into ``agent_bff.domain.db``."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    db_module._engine = engine
    db_module.set_session_factory(factory)
    try:
        yield factory
    finally:
        db_module.set_session_factory(None)
        db_module._engine = None
        await engine.dispose()


@contextmanager
def app_client(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """TestClient whose lifespan leaves the test-provided DB factory alone."""
    from agent_bff import main as main_module

    async def _noop_init(*_a: object, **_kw: object) -> None:
        return None

    async def _noop_close() -> None:
        return None

    monkeypatch.setattr(main_module, "init_db", _noop_init)
    monkeypatch.setattr(main_module, "close_db", _noop_close)
    with TestClient(create_app(settings), raise_server_exceptions=False) as client:
        yield client


def run_db[T](
    client: TestClient,
    factory: async_sessionmaker[AsyncSession],
    fn: Callable[[AsyncSession], Awaitable[T]],
) -> T:
    """Run ``fn`` in a committed DB transaction on the app's event loop."""

    async def _go() -> T:
        async with factory() as session, session.begin():
            return await fn(session)

    assert client.portal is not None, "use the TestClient as a context manager"
    return client.portal.call(_go)


def seed_rows(
    client: TestClient,
    factory: async_sessionmaker[AsyncSession],
    user_id: str,
    *,
    tenant_id: str = TENANT,
    ack_versions: tuple[str, ...] = ("2026-09.aaaaaaaaaaaa", "2026-10.bbbbbbbbbbbb"),
    event_key: str = "task_completed",
) -> str:
    """One notification preference, one API key and ``ack_versions`` for a user.

    Returns the API key's stored hash, so tests can check it never leaks.
    """
    key_hash = hashlib.sha256(f"{tenant_id}:{user_id}".encode()).hexdigest()

    async def _seed(session: AsyncSession) -> None:
        session.add(NotificationPreference(
            tenant_id=tenant_id, user_id=user_id, event_key=event_key,
            email_enabled=False, push_enabled=True,
        ))
        session.add(ApiKey(
            tenant_id=tenant_id, user_id=user_id, name="CI pipeline",
            key_prefix=f"ap_{user_id[-6:]}", key_hash=key_hash,
        ))
        for version in ack_versions:
            session.add(PrivacyNoticeAcknowledgement(
                tenant_id=tenant_id, user_id=user_id, notice_version=version,
            ))

    run_db(client, factory, _seed)
    return key_hash


def count_rows(
    client: TestClient,
    factory: async_sessionmaker[AsyncSession],
    user_id: str,
    *,
    tenant_id: str = TENANT,
) -> dict[str, int]:
    async def _count(session: AsyncSession) -> dict[str, int]:
        counts: dict[str, int] = {}
        for model in (NotificationPreference, ApiKey, PrivacyNoticeAcknowledgement):
            counts[model.__tablename__] = (
                await session.execute(
                    select(func.count())
                    .select_from(model)
                    .where(model.tenant_id == tenant_id, model.user_id == user_id)
                )
            ).scalar_one()
        return counts

    return run_db(client, factory, _count)
