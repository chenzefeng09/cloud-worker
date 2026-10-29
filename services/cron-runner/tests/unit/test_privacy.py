"""Trigger ownership + the privacy fan-out endpoints (GDPR export / erasure).

Same harness as ``test_triggers.py``: the app over an ASGI transport against a
file-backed SQLite DB (no lifespan, so no real APScheduler — a stub is put on
``app.state`` where a test needs one).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import func, select

from cron_runner import scheduler_service
from cron_runner.config import Settings
from cron_runner.domain import db as db_mod
from cron_runner.domain.models import Base, CronJob
from cron_runner.main import create_app

API = "/api/cron/v1/cron-jobs"
PRIVACY = "/api/cron/v1/internal/privacy/users/{user}/cron-jobs"
TENANT = "tenant_a"
OWNER = "usr_01J5TEST0000000000000USER1"
OTHER = "usr_01J5TEST0000000000000USER2"
SVC = {"X-Service-Token": "test_token", "X-Tenant-Id": TENANT}


class _StubScheduler:
    def __init__(self) -> None:
        self.removed: list[str] = []

    def add_job(self, func: Any, trigger: Any, id: str, replace_existing: bool) -> None:  # noqa: A002
        pass

    def remove_job(self, job_id: str) -> None:
        self.removed.append(job_id)


@pytest_asyncio.fixture
async def app_and_client(tmp_path) -> AsyncIterator[tuple[Any, httpx.AsyncClient]]:
    dsn = f"sqlite+aiosqlite:///{tmp_path}/privacy.db"
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
        yield app, c

    await db_mod.close_db()


@pytest.fixture
def client(app_and_client: tuple[Any, httpx.AsyncClient]) -> httpx.AsyncClient:
    return app_and_client[1]


async def _create(
    client: httpx.AsyncClient,
    *,
    user: str | None = OWNER,
    tenant: str = TENANT,
    name: str = "daily",
    webhook: bool = False,
) -> dict[str, Any]:
    headers = {"X-Tenant-Id": tenant}
    if user is not None:
        headers["X-User-Id"] = user
    body: dict[str, Any] = {"app_id": "app_1", "name": name}
    if webhook:
        body.update(trigger_type="webhook", field_mapping={"a": "b"})
    else:
        body["cron_expression"] = "0 * * * *"
    resp = await client.post(API, headers=headers, json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _count_jobs() -> int:
    n = 0
    async for session in db_mod.get_session(tenant_id="SYSTEM", actor="SYSTEM"):
        n = int((await session.execute(select(func.count()).select_from(CronJob))).scalar_one())
    return n


# --------------------------------------------------------------------------- #
# ownership
# --------------------------------------------------------------------------- #

async def test_create_records_the_calling_user_as_owner(client: httpx.AsyncClient) -> None:
    job = await _create(client)
    assert job["created_by_user_id"] == OWNER

    [listed] = (await client.get(API, headers={"X-Tenant-Id": TENANT})).json()
    assert listed["created_by_user_id"] == OWNER


async def test_create_without_a_user_records_no_owner(client: httpx.AsyncClient) -> None:
    job = await _create(client, user=None)
    assert job["created_by_user_id"] is None


async def test_patch_never_changes_the_owner(client: httpx.AsyncClient) -> None:
    job = await _create(client)
    resp = await client.patch(
        f"{API}/{job['id']}",
        headers={"X-Tenant-Id": TENANT, "X-User-Id": OTHER},
        json={"name": "renamed", "created_by_user_id": OTHER},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["name"] == "renamed"
    assert resp.json()["created_by_user_id"] == OWNER


async def test_fire_names_the_owner_as_on_behalf_of(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    owned = await _create(client, name="owned")
    unowned = await _create(client, user=None, name="system")
    posted: list[dict[str, Any]] = []

    class _FakeBff:
        def __init__(self, *_a: Any, **_kw: Any) -> None:
            pass

        async def __aenter__(self) -> _FakeBff:
            return self

        async def __aexit__(self, *_exc: Any) -> None:
            return None

        async def get(self, url: str, **kw: Any) -> httpx.Response:
            return httpx.Response(200, json={"versions": [{"status": "published"}]})

        async def post(self, url: str, **kw: Any) -> httpx.Response:
            posted.append({"url": url, "json": kw["json"], "headers": kw["headers"]})
            return httpx.Response(201, json={"session_id": "ses_1", "run_id": "run_1"})

    monkeypatch.setattr(scheduler_service.httpx, "AsyncClient", _FakeBff)
    settings = Settings(
        db_dsn="sqlite+aiosqlite:///",
        db_dsn_migrator="sqlite+aiosqlite:///",
        bff_url="http://test-bff:8005",
        service_token="test_token",
    )
    await scheduler_service._do_fire(owned["id"], settings)
    await scheduler_service._do_fire(unowned["id"], settings)

    first, second = posted
    assert first["json"]["on_behalf_of"] == OWNER
    # The run itself still executes as the service account (no authz change).
    assert first["headers"]["X-User-Id"] == settings.service_user_id
    assert "on_behalf_of" not in second["json"]


# --------------------------------------------------------------------------- #
# internal privacy endpoints
# --------------------------------------------------------------------------- #

async def test_list_returns_only_the_users_triggers_in_the_tenant(
    client: httpx.AsyncClient,
) -> None:
    mine = await _create(client, name="mine")
    hook = await _create(client, name="hook", webhook=True)
    await _create(client, user=OTHER, name="theirs")
    await _create(client, tenant="tenant_b", name="other tenant")

    resp = await client.get(PRIVACY.format(user=OWNER), headers=SVC)

    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert [i["id"] for i in items] == [mine["id"], hook["id"]]
    assert {i["tenant_id"] for i in items} == {TENANT}
    by_id = {i["id"]: i for i in items}
    assert by_id[hook["id"]]["has_webhook_secret"] is True
    assert "webhook_secret_hash" not in resp.text
    assert "webhook_secret" not in by_id[hook["id"]]


async def test_delete_removes_and_unschedules_only_the_users_triggers(
    app_and_client: tuple[Any, httpx.AsyncClient],
) -> None:
    app, client = app_and_client
    mine = await _create(client, name="mine")
    hook = await _create(client, name="hook", webhook=True)
    theirs = await _create(client, user=OTHER, name="theirs")
    elsewhere = await _create(client, tenant="tenant_b", name="other tenant")
    scheduler = _StubScheduler()
    app.state.scheduler = scheduler

    resp = await client.delete(PRIVACY.format(user=OWNER), headers=SVC)

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"deleted": 2}
    assert sorted(scheduler.removed) == sorted([mine["id"], hook["id"]])
    remaining = (await client.get(API, headers={"X-Tenant-Id": TENANT})).json()
    assert [j["id"] for j in remaining] == [theirs["id"]]
    other_tenant = (await client.get(API, headers={"X-Tenant-Id": "tenant_b"})).json()
    assert [j["id"] for j in other_tenant] == [elsewhere["id"]]

    again = await client.delete(PRIVACY.format(user=OWNER), headers=SVC)
    assert again.status_code == 200
    assert again.json() == {"deleted": 0}


@pytest.mark.parametrize("method", ["GET", "DELETE"])
@pytest.mark.parametrize(
    "headers",
    [
        {"X-Tenant-Id": TENANT},
        {"X-Tenant-Id": TENANT, "X-Service-Token": "wrong"},
        {"X-Service-Token": "test_token"},
    ],
)
async def test_privacy_endpoints_require_the_service_token_and_tenant(
    client: httpx.AsyncClient, method: str, headers: dict[str, str],
) -> None:
    await _create(client)
    resp = await client.request(method, PRIVACY.format(user=OWNER), headers=headers)
    assert resp.status_code == 401
    assert await _count_jobs() == 1
