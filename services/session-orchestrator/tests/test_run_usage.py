"""A run's token usage is recorded wherever one of its legs ends.

The worker reports the whole run's cumulative usage (a resumed leg counts on
from where its paused leg stopped), so the orchestrator applies every
observation as a maximum: the pause, the end, a cancel, a reconcile repeated or
out of order can neither count a leg twice nor lower the total.  A cancelled run
is never polled again, so the cancel records what the scheduler read when it
stopped the sandbox.

These run against a real (SQLite) database so the comparison inside the UPDATE
is exercised, not mocked.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update

from agent_session_orchestrator.api.v1.messages import _reconcile_run_completion
from agent_session_orchestrator.domain.models import Run, Session
from agent_session_orchestrator.services.session_service import SessionService
from tests._helpers import TEST_TENANT, TEST_USER, AuthzAwareClient
from tests._privacy_harness import new_db

LEG1 = {"input_tokens": 100, "output_tokens": 40, "total_tokens": 140}
# Cumulative: the resumed leg counted on from LEG1.
WHOLE_RUN = {"input_tokens": 250, "output_tokens": 90, "total_tokens": 340}


def _tokens(run: Run) -> tuple[int | None, int | None, int | None]:
    return run.input_tokens, run.output_tokens, run.total_tokens


@pytest.fixture
def db() -> Iterator[Any]:
    yield new_db()


def _seed(sync: Any, *, status: str = "RUNNING", sandbox_id: str = "sb_leg1",
          tokens: dict[str, int] | None = None) -> Run:
    sync.add(Session(id="ses_1", tenant_id=TEST_TENANT, user_id=TEST_USER, status="ACTIVE"))
    sync.commit()
    run = Run(id="run_1", tenant_id=TEST_TENANT, session_id="ses_1", task_description="t",
              status=status, sandbox_id=sandbox_id, **(tokens or {}))
    sync.add(run)
    sync.commit()
    return run


# ── the primitive ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_usage_only_ever_rises(db: Any) -> None:
    session, sync = db
    run = _seed(sync)
    svc = SessionService(session, tenant_id=TEST_TENANT)

    await svc.raise_run_usage(run, LEG1)
    assert _tokens(run) == (100, 40, 140)

    # Repeated, or an older observation arriving late: nothing is lowered.
    await svc.raise_run_usage(run, LEG1)
    await svc.raise_run_usage(run, {"input_tokens": 90, "output_tokens": 45, "total_tokens": 135})
    assert _tokens(run) == (100, 45, 140)

    await svc.raise_run_usage(run, None)
    await svc.raise_run_usage(run, WHOLE_RUN)
    sync.commit()
    sync.expire_all()
    assert _tokens(sync.get(Run, "run_1")) == (250, 90, 340)


@pytest.mark.asyncio
async def test_a_stale_in_memory_run_cannot_lower_the_stored_count(db: Any) -> None:
    session, sync = db
    run = _seed(sync, tokens=LEG1)
    # Another transaction raised it after this one loaded the row.
    sync.execute(
        update(Run).where(Run.id == "run_1").values(input_tokens=900)
        .execution_options(synchronize_session=False),
    )
    sync.commit()
    assert run.input_tokens == 100  # stale

    await SessionService(session, tenant_id=TEST_TENANT).raise_run_usage(
        run, {"input_tokens": 300, "output_tokens": 40},
    )
    assert run.input_tokens == 900


@pytest.mark.asyncio
async def test_unusable_counts_are_dropped_and_the_total_derived(db: Any) -> None:
    session, sync = db
    run = _seed(sync)
    svc = SessionService(session, tenant_id=TEST_TENANT)

    # The record comes from a file the agent can write: nothing here may fail
    # the transaction that records it.
    for bad in ("lots", ["x"], {"input_tokens": "9", "output_tokens": -1, "total_tokens": 2**40},
                {"input_tokens": True, "output_tokens": 1.5}):
        await svc.raise_run_usage(run, bad)
    assert _tokens(run) == (None, None, None)

    await svc.raise_run_usage(run, {"input_tokens": 10, "output_tokens": 5})
    assert _tokens(run) == (10, 5, 15)


# ── reconcile: a paused, resumed and finished run ─────────────────────────────


class _Scheduler:
    def __init__(self) -> None:
        self.payload: dict[str, Any] = {}

    def __call__(self, *a: Any, **kw: Any) -> _Scheduler:
        return self

    async def __aenter__(self) -> _Scheduler:
        return self

    async def __aexit__(self, *a: Any) -> None:
        return None

    async def get(self, url: str, **kw: Any) -> httpx.Response:
        return httpx.Response(200, json=self.payload, request=httpx.Request("GET", url))


async def _reconcile(session: Any, run: Run, scheduler: _Scheduler) -> None:
    with patch.object(httpx, "AsyncClient", scheduler):
        await _reconcile_run_completion(
            scheduler_url="http://sched", tenant_id=TEST_TENANT, user_id=TEST_USER,
            run=run, svc=SessionService(session, tenant_id=TEST_TENANT),
        )


@pytest.mark.asyncio
async def test_a_paused_and_resumed_run_ends_with_the_whole_runs_usage(db: Any) -> None:
    session, sync = db
    run = _seed(sync)
    scheduler = _Scheduler()

    # Leg 1 pauses for input.
    scheduler.payload = {"status": "COMPLETED", "result_status": "paused", "usage": LEG1}
    await _reconcile(session, run, scheduler)
    sync.commit()
    assert run.status == "PAUSED"
    assert _tokens(run) == (100, 40, 140)

    # Resumed into a new sandbox; leg 2 finishes.
    run.status, run.sandbox_id = "RUNNING", "sb_leg2"
    sync.commit()
    scheduler.payload = {
        "status": "COMPLETED", "result_status": "OK", "result_summary": "done",
        "usage": WHOLE_RUN,
    }
    await _reconcile(session, run, scheduler)
    sync.commit()
    sync.expire_all()
    stored = sync.get(Run, "run_1")
    assert stored.status == "COMPLETED"
    assert _tokens(stored) == (250, 90, 340)


@pytest.mark.asyncio
async def test_an_end_without_usage_keeps_what_the_pause_recorded(db: Any) -> None:
    session, sync = db
    run = _seed(sync, tokens=LEG1)
    scheduler = _Scheduler()
    scheduler.payload = {"status": "FAILED", "result_status": "FAILED", "result_error": "boom"}
    await _reconcile(session, run, scheduler)
    sync.commit()
    assert run.status == "FAILED"
    assert _tokens(run) == (100, 40, 140)


# ── cancel ────────────────────────────────────────────────────────────────────


class _CancelUpstreams(AuthzAwareClient):
    def __init__(self) -> None:
        self.cancel_body: Any = {"id": "run_1", "status": "CANCELLED"}
        self.cancelled: list[str] = []

    def __call__(self, *a: Any, **kw: Any) -> _CancelUpstreams:
        return self

    async def post(self, url: str, **kw: Any) -> httpx.Response:
        if "/check" in url and "authz" in url:
            return self._authz_allowed_resp()
        self.cancelled.append(url)
        return httpx.Response(200, json=self.cancel_body)


@pytest.fixture
def cancel_client(
    app_client: TestClient, db: Any,
) -> Iterator[tuple[TestClient, _CancelUpstreams]]:
    from agent_session_orchestrator.api.dependencies import db_session

    session, sync = db

    async def _transaction() -> AsyncIterator[Any]:
        try:
            yield session
        except BaseException:
            sync.rollback()
            raise
        sync.commit()

    upstreams = _CancelUpstreams()
    app_client.app.dependency_overrides[db_session] = _transaction
    with patch.object(httpx, "AsyncClient", upstreams):
        yield app_client, upstreams


CANCEL = "/api/session/v1/sessions/ses_1/runs/run_1/cancel"


def test_a_cancelled_run_records_what_it_used_up_to_the_stop(
    cancel_client: tuple[TestClient, _CancelUpstreams], db: Any, auth_headers: dict[str, str],
) -> None:
    client, upstreams = cancel_client
    _, sync = db
    _seed(sync)
    upstreams.cancel_body = {"id": "run_1", "status": "CANCELLED", "usage": WHOLE_RUN}

    resp = client.post(CANCEL, headers=auth_headers)

    assert resp.status_code == 200, resp.text
    assert [url.rsplit("/runs/", 1)[1] for url in upstreams.cancelled] == ["sb_leg1/cancel"]
    sync.expire_all()
    stored = sync.get(Run, "run_1")
    assert stored.status == "CANCELLED"
    assert _tokens(stored) == (250, 90, 340)


def test_cancelling_a_paused_run_does_not_count_its_pause_twice(
    cancel_client: tuple[TestClient, _CancelUpstreams], db: Any, auth_headers: dict[str, str],
) -> None:
    client, upstreams = cancel_client
    _, sync = db
    _seed(sync, status="PAUSED", tokens=LEG1)
    # The paused leg's sandbox still holds the file its pause was recorded from.
    upstreams.cancel_body = {"id": "run_1", "status": "CANCELLED", "usage": LEG1}

    assert client.post(CANCEL, headers=auth_headers).status_code == 200

    sync.expire_all()
    assert _tokens(sync.get(Run, "run_1")) == (100, 40, 140)


@pytest.mark.parametrize("body", [{"id": "run_1", "status": "CANCELLED"}, "not json", None])
def test_a_cancel_without_usage_keeps_the_recorded_count(
    cancel_client: tuple[TestClient, _CancelUpstreams], db: Any, auth_headers: dict[str, str],
    body: Any,
) -> None:
    client, upstreams = cancel_client
    _, sync = db
    _seed(sync, tokens=LEG1)
    upstreams.cancel_body = body

    assert client.post(CANCEL, headers=auth_headers).status_code == 200

    sync.expire_all()
    stored = sync.get(Run, "run_1")
    assert stored.status == "CANCELLED"
    assert _tokens(stored) == (100, 40, 140)
