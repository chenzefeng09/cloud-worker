"""Trigger (schedule | webhook) CRUD + scheduler-guard tests.

Runs the FastAPI app over an ASGI transport against a file-backed SQLite DB.
ASGITransport does not fire the lifespan, so no real APScheduler/BFF is
started; ``_get_scheduler`` returns None and the create/update handlers skip
scheduling. The scheduler-guard test exercises ``load_all_jobs`` directly with
a stub scheduler.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator

import httpx
import pytest_asyncio
from sqlalchemy import select

from cron_runner.config import Settings
from cron_runner.domain import db as db_mod
from cron_runner.domain.models import Base, CronJob
from cron_runner.main import create_app

API = "/api/cron/v1/cron-jobs"
VERIFY = "/api/cron/v1/internal/cron-jobs/{id}/verify"
HEADERS = {"X-Tenant-Id": "tenant_a", "X-User-Id": "usr_test"}
# The client fixture configures service_token="test_token".
SVC_HEADERS = {"X-Service-Token": "test_token"}


@pytest_asyncio.fixture
async def client(tmp_path) -> AsyncIterator[httpx.AsyncClient]:
    dsn = f"sqlite+aiosqlite:///{tmp_path}/triggers.db"
    settings = Settings(
        db_dsn=dsn,
        db_dsn_migrator=dsn,
        bff_url="http://test-bff:8005",
        service_token="test_token",
    )
    await db_mod.init_db(settings)
    assert db_mod._engine is not None
    async with db_mod._engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    app = create_app(settings)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c

    await db_mod.close_db()


# --------------------------------------------------------------------------- #
# schedule triggers — cron_expression stays required
# --------------------------------------------------------------------------- #

async def test_schedule_create_requires_cron_expression(client: httpx.AsyncClient) -> None:
    resp = await client.post(
        API, headers=HEADERS, json={"app_id": "app_1", "name": "no-cron"}
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["code"] == "CRON.INVALID_TRIGGER"


async def test_schedule_create_succeeds_and_hides_secret_fields(
    client: httpx.AsyncClient,
) -> None:
    resp = await client.post(
        API,
        headers=HEADERS,
        json={"app_id": "app_1", "name": "daily", "cron_expression": "0 * * * *"},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["trigger_type"] == "schedule"
    assert data["cron_expression"] == "0 * * * *"
    assert data["has_webhook_secret"] is False
    # Schedule rows carry the additive secret fields as null.
    assert data["webhook_secret"] is None
    assert data["webhook_path"] is None
    # Backward-compat fields the BFF/frontend depend on are all present.
    for key in (
        "id", "tenant_id", "app_id", "name", "timezone", "input_values",
        "enabled", "source", "last_run_at", "next_run_at", "last_status",
        "last_error", "created_at", "updated_at",
    ):
        assert key in data


# --------------------------------------------------------------------------- #
# webhook triggers — no cron, field_mapping required, secret generated once
# --------------------------------------------------------------------------- #

async def test_webhook_create_requires_field_mapping(client: httpx.AsyncClient) -> None:
    resp = await client.post(
        API,
        headers=HEADERS,
        json={"app_id": "app_1", "name": "hook", "trigger_type": "webhook"},
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "CRON.INVALID_TRIGGER"


async def test_webhook_create_succeeds_without_cron_and_returns_secret_once(
    client: httpx.AsyncClient,
) -> None:
    resp = await client.post(
        API,
        headers=HEADERS,
        json={
            "app_id": "app_1",
            "name": "hook",
            "trigger_type": "webhook",
            "field_mapping": {"incoming_key": "app_field"},
        },
    )
    assert resp.status_code == 201
    data = resp.json()
    job_id = data["id"]

    assert data["trigger_type"] == "webhook"
    assert data["cron_expression"] is None
    assert data["field_mapping"] == {"incoming_key": "app_field"}
    assert data["has_webhook_secret"] is True

    # Plaintext + path returned exactly once on create.
    plaintext = data["webhook_secret"]
    assert isinstance(plaintext, str) and plaintext
    assert data["webhook_path"] == f"/api/bff/v1/triggers/{job_id}/webhook"

    # List/get responses must NEVER include the plaintext secret.
    lst = await client.get(API, headers=HEADERS)
    assert lst.status_code == 200
    row = next(r for r in lst.json() if r["id"] == job_id)
    assert "webhook_secret" not in row
    assert "webhook_secret_hash" not in row
    assert row["has_webhook_secret"] is True
    assert row["cron_expression"] is None

    # Stored value is a SHA-256 hash of the plaintext — not the plaintext.
    async for session in db_mod.get_session(tenant_id="tenant_a", user_id="usr_test"):
        result = await session.execute(select(CronJob).where(CronJob.id == job_id))
        job = result.scalar_one()
        assert job.webhook_secret_hash != plaintext
        assert job.webhook_secret_hash == hashlib.sha256(
            plaintext.encode("utf-8")
        ).hexdigest()


async def test_rotate_secret_returns_new_plaintext_and_updates_hash(
    client: httpx.AsyncClient,
) -> None:
    created = await client.post(
        API,
        headers=HEADERS,
        json={
            "app_id": "app_1",
            "name": "hook",
            "trigger_type": "webhook",
            "field_mapping": {"a": "b"},
        },
    )
    job_id = created.json()["id"]
    first_plaintext = created.json()["webhook_secret"]

    rot = await client.post(f"{API}/{job_id}/rotate-secret", headers=HEADERS)
    assert rot.status_code == 200
    rotated = rot.json()
    assert rotated["id"] == job_id
    assert rotated["webhook_secret"] and rotated["webhook_secret"] != first_plaintext
    assert rotated["webhook_path"] == f"/api/bff/v1/triggers/{job_id}/webhook"

    async for session in db_mod.get_session(tenant_id="tenant_a", user_id="usr_test"):
        result = await session.execute(select(CronJob).where(CronJob.id == job_id))
        job = result.scalar_one()
        assert job.webhook_secret_hash == hashlib.sha256(
            rotated["webhook_secret"].encode("utf-8")
        ).hexdigest()


async def test_rotate_secret_rejects_schedule_trigger(client: httpx.AsyncClient) -> None:
    created = await client.post(
        API,
        headers=HEADERS,
        json={"app_id": "app_1", "name": "daily", "cron_expression": "0 * * * *"},
    )
    job_id = created.json()["id"]
    rot = await client.post(f"{API}/{job_id}/rotate-secret", headers=HEADERS)
    assert rot.status_code == 400
    assert rot.json()["code"] == "CRON.INVALID_TRIGGER"


# --------------------------------------------------------------------------- #
# scheduler guard — load_all_jobs only picks up schedule rows
# --------------------------------------------------------------------------- #

class _StubScheduler:
    def __init__(self) -> None:
        self.added: list[str] = []

    def add_job(self, func, trigger, id, replace_existing) -> None:  # noqa: ANN001
        self.added.append(id)

    def remove_job(self, job_id: str) -> None:  # pragma: no cover - unused here
        pass


async def test_load_all_jobs_skips_webhook_rows(client: httpx.AsyncClient) -> None:
    # ``client`` fixture initialized the DB + tables; seed one of each type.
    async for session in db_mod.get_session(
        tenant_id="SYSTEM", user_id="svc-cron", actor="SYSTEM"
    ):
        session.add(
            CronJob(
                tenant_id="tenant_a",
                app_id="app_1",
                name="sched",
                trigger_type="schedule",
                cron_expression="0 * * * *",
                enabled=True,
            )
        )
        session.add(
            CronJob(
                tenant_id="tenant_a",
                app_id="app_1",
                name="hook",
                trigger_type="webhook",
                cron_expression=None,
                field_mapping={"a": "b"},
                webhook_secret_hash="deadbeef",
                enabled=True,
            )
        )

    from cron_runner.scheduler_service import load_all_jobs

    scheduler = _StubScheduler()
    settings = Settings(
        db_dsn="sqlite+aiosqlite:///",
        db_dsn_migrator="sqlite+aiosqlite:///",
        bff_url="http://test-bff:8005",
        service_token="test_token",
    )
    await load_all_jobs(scheduler, settings)  # type: ignore[arg-type]

    assert len(scheduler.added) == 1  # only the schedule row was loaded


# --------------------------------------------------------------------------- #
# internal verify endpoint — secret exchange for the BFF webhook receiver
# --------------------------------------------------------------------------- #

async def _create_webhook(
    client: httpx.AsyncClient, input_values: dict | None = None
) -> tuple[str, str]:
    """Create a webhook trigger; return (job_id, plaintext_secret)."""
    payload: dict = {
        "app_id": "app_1",
        "name": "hook",
        "trigger_type": "webhook",
        "field_mapping": {"incoming_key": "app_field"},
    }
    if input_values is not None:
        payload["input_values"] = input_values
    resp = await client.post(API, headers=HEADERS, json=payload)
    assert resp.status_code == 201
    data = resp.json()
    return data["id"], data["webhook_secret"]


async def test_verify_match_returns_context(client: httpx.AsyncClient) -> None:
    # A mixed-input webhook: one fixed text field, mapped from-event field.
    input_values = {"text": {"tone": "formal"}, "multi": {}}
    job_id, secret = await _create_webhook(client, input_values=input_values)
    resp = await client.post(
        VERIFY.format(id=job_id), headers=SVC_HEADERS, json={"secret": secret}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == {
        "id": job_id,
        "tenant_id": "tenant_a",
        "app_id": "app_1",
        "field_mapping": {"incoming_key": "app_field"},
        "input_values": input_values,
        "trigger_type": "webhook",
    }


async def test_verify_wrong_secret_returns_401(client: httpx.AsyncClient) -> None:
    job_id, _secret = await _create_webhook(client)
    resp = await client.post(
        VERIFY.format(id=job_id), headers=SVC_HEADERS, json={"secret": "nope"}
    )
    assert resp.status_code == 401


async def test_verify_schedule_trigger_returns_404(client: httpx.AsyncClient) -> None:
    created = await client.post(
        API,
        headers=HEADERS,
        json={"app_id": "app_1", "name": "daily", "cron_expression": "0 * * * *"},
    )
    job_id = created.json()["id"]
    resp = await client.post(
        VERIFY.format(id=job_id), headers=SVC_HEADERS, json={"secret": "whatever"}
    )
    assert resp.status_code == 404


async def test_verify_unknown_id_returns_404(client: httpx.AsyncClient) -> None:
    resp = await client.post(
        VERIFY.format(id="cron_DOESNOTEXIST"),
        headers=SVC_HEADERS,
        json={"secret": "whatever"},
    )
    assert resp.status_code == 404


async def test_verify_bad_service_token_returns_401(client: httpx.AsyncClient) -> None:
    job_id, secret = await _create_webhook(client)
    # Correct secret but wrong/missing service token → 401 (auth precedes lookup).
    resp = await client.post(
        VERIFY.format(id=job_id),
        headers={"X-Service-Token": "wrong"},
        json={"secret": secret},
    )
    assert resp.status_code == 401
    resp_missing = await client.post(
        VERIFY.format(id=job_id), json={"secret": secret}
    )
    assert resp_missing.status_code == 401
