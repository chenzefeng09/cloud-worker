"""A user turn starts atomically: its message is written with its run.

``POST /sessions/{id}/runs`` may carry the turn's ``message``.  It is written
in the run's transaction, so a run that is refused (another one is live, the
caller may not post messages) or that the scheduler cannot start leaves no
unanswered message behind.  These tests run the endpoint against a real
(SQLite) database whose request transaction commits or rolls back exactly like
``domain.db.get_session``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from agent_session_orchestrator.domain.models import Message, Run, Session
from tests._helpers import TEST_TENANT, TEST_USER, AuthzAwareClient
from tests._privacy_harness import new_db

URL = "/api/session/v1/sessions/ses_1/runs"


class _Upstreams(AuthzAwareClient):
    """authz + agent-scheduler: records dispatches, answers with ``scheduler_status``."""

    def __init__(self) -> None:
        self.scheduler_status = 201
        self.scheduler_body: dict[str, Any] = {"sandbox_id": "sb_new_000000"}
        self.denied_actions: set[str] = set()
        self.dispatched: list[dict[str, Any]] = []

    def __call__(self, *a: Any, **kw: Any) -> _Upstreams:
        return self

    async def post(self, url: str, **kw: Any) -> httpx.Response:
        if "/check" in url and "authz" in url:
            allowed = kw["json"]["action"] not in self.denied_actions
            return httpx.Response(200, json={"allowed": allowed})
        if url.endswith("/runs"):
            self.dispatched.append(kw["json"])
            return httpx.Response(self.scheduler_status, json=self.scheduler_body)
        return httpx.Response(404, json={})


@pytest.fixture
def db() -> Iterator[Any]:
    yield new_db()


@pytest.fixture
def upstreams() -> _Upstreams:
    return _Upstreams()


@pytest.fixture
def client(app_client: TestClient, db: Any, upstreams: _Upstreams) -> Iterator[TestClient]:
    from agent_session_orchestrator.api.dependencies import db_session

    session, sync = db

    async def _transaction() -> AsyncIterator[Any]:
        # ``get_session`` runs the request in ``session.begin()``: commit on
        # success, roll back on any exception (HTTPException included).
        try:
            yield session
        except BaseException:
            sync.rollback()
            raise
        sync.commit()

    app_client.app.dependency_overrides[db_session] = _transaction
    with (
        patch.object(httpx, "AsyncClient", upstreams),
        patch("agent_session_orchestrator.api.v1.runs._fetch_memory",
              new=AsyncMock(return_value="")),
        patch("agent_session_orchestrator.api.v1.runs._fetch_knowledge",
              new=AsyncMock(return_value="")),
        patch("agent_session_orchestrator.api.v1.runs._fetch_default_installed_skills",
              new=AsyncMock(return_value=[])),
        patch("agent_session_orchestrator.api.v1.runs._fetch_platform_default_skills",
              new=AsyncMock(return_value=[])),
    ):
        yield app_client


def _seed(sync: Any, *turns: tuple[str, str], live_run: bool = False) -> None:
    sync.add(Session(id="ses_1", tenant_id=TEST_TENANT, user_id=TEST_USER, status="ACTIVE"))
    sync.commit()
    for seq, (role, content) in enumerate(turns, start=1):
        sync.add(Message(tenant_id=TEST_TENANT, session_id="ses_1", role=role,
                         content=content, sequence=seq))
    if live_run:
        sync.add(Run(id="run_live", tenant_id=TEST_TENANT, session_id="ses_1",
                     task_description="busy", status="RUNNING"))
    sync.commit()


def _messages(sync: Any) -> list[tuple[str, str]]:
    rows = sync.execute(
        select(Message).where(Message.session_id == "ses_1").order_by(Message.sequence),
    ).scalars().all()
    return [(m.role, m.content) for m in rows]


def _runs(sync: Any) -> list[str]:
    return [r.task_description for r in sync.execute(select(Run)).scalars().all()]


def _turn(text: str, **extra: Any) -> dict[str, Any]:
    return {"task": text, "message": {"content": text, "attachment_ids": []}, **extra}


def test_the_message_is_written_with_the_run_and_kept_out_of_history(
    client: TestClient, db: Any, upstreams: _Upstreams, auth_headers: dict[str, str],
) -> None:
    _, sync = db
    _seed(sync, ("USER", "again"), ("ASSISTANT", "first answer"))

    resp = client.post(URL, headers=auth_headers, json=_turn("again"))

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["message"]["role"] == "USER"
    assert body["message"]["content"] == "again"
    assert body["message"]["sequence"] == 3
    # The USER row is not tied to the run: the stream tells it apart from the
    # run's own output by that.
    assert body["message"]["run_id"] is None
    assert _messages(sync) == [("USER", "again"), ("ASSISTANT", "first answer"), ("USER", "again")]
    assert _runs(sync) == ["again"]
    assert sync.get(Session, "ses_1").current_run_id == body["id"]
    # History is the earlier turn, the identical earlier question included.
    (dispatch,) = upstreams.dispatched
    assert [(h["role"], h["content"]) for h in dispatch["history"]] == [
        ("user", "again"), ("assistant", "first answer"),
    ]


@pytest.mark.parametrize("scheduler_status", [500, 503])
def test_a_run_the_scheduler_refuses_leaves_no_message(
    client: TestClient, db: Any, upstreams: _Upstreams, auth_headers: dict[str, str],
    scheduler_status: int,
) -> None:
    _, sync = db
    _seed(sync, ("USER", "earlier"), ("ASSISTANT", "answer"))
    upstreams.scheduler_status = scheduler_status

    resp = client.post(URL, headers=auth_headers, json=_turn("hello"))

    assert resp.status_code == 502
    assert _messages(sync) == [("USER", "earlier"), ("ASSISTANT", "answer")]
    assert _runs(sync) == []
    assert sync.get(Session, "ses_1").current_run_id is None


# A refusal the user can act on keeps its meaning (and stays non-retryable);
# the scheduler's own wording is not echoed.
@pytest.mark.parametrize(("scheduler_status", "scheduler_body", "status", "code", "says"), [
    (422, {"code": "AGENT.SCHEDULER.LLM_NOT_CONFIGURED",
           "detail": "No enabled LLM provider is configured for this tenant"},
     422, "AGENT.RUN.LLM_NOT_CONFIGURED", "No AI model is set up for your organization"),
    (402, {"detail": {"message": "quota exceeded", "dimension": "agent_sessions"}},
     402, "AGENT.RUN.QUOTA_EXCEEDED", "reached its limit for agent runs"),
])
def test_a_refusal_the_user_can_act_on_says_what_to_do(
    client: TestClient, db: Any, upstreams: _Upstreams, auth_headers: dict[str, str],
    scheduler_status: int, scheduler_body: dict[str, Any], status: int, code: str, says: str,
) -> None:
    _, sync = db
    _seed(sync, ("USER", "earlier"), ("ASSISTANT", "answer"))
    upstreams.scheduler_status = scheduler_status
    upstreams.scheduler_body = scheduler_body

    resp = client.post(URL, headers=auth_headers, json=_turn("hello"))

    assert resp.status_code == status, resp.text
    assert resp.json()["code"] == code
    assert says in resp.json()["user_message"]
    assert _messages(sync) == [("USER", "earlier"), ("ASSISTANT", "answer")]
    assert _runs(sync) == []
    assert sync.get(Session, "ses_1").current_run_id is None


def test_an_unknown_scheduler_refusal_is_not_echoed(
    client: TestClient, db: Any, upstreams: _Upstreams, auth_headers: dict[str, str],
) -> None:
    _, sync = db
    _seed(sync)
    upstreams.scheduler_status = 500
    upstreams.scheduler_body = {"code": "AGENT.SCHEDULER.SPAWN_FAILED",
                                "detail": "docker: /var/run/docker.sock refused"}

    resp = client.post(URL, headers=auth_headers, json=_turn("hello"))

    assert resp.status_code == 502
    assert "docker" not in resp.text
    assert resp.json()["detail"] == "scheduler rejected run (HTTP 500)"


def test_a_second_live_run_is_refused_without_writing_the_message(
    client: TestClient, db: Any, upstreams: _Upstreams, auth_headers: dict[str, str],
) -> None:
    _, sync = db
    _seed(sync, ("USER", "busy"), live_run=True)

    resp = client.post(URL, headers=auth_headers, json=_turn("and another thing"))

    assert resp.status_code == 409
    assert _messages(sync) == [("USER", "busy")]
    assert _runs(sync) == ["busy"]
    assert upstreams.dispatched == []


def test_posting_the_message_needs_the_message_grant(
    client: TestClient, db: Any, upstreams: _Upstreams, auth_headers: dict[str, str],
) -> None:
    _, sync = db
    _seed(sync)
    upstreams.denied_actions = {"session.message.create"}

    resp = client.post(URL, headers=auth_headers, json=_turn("hello"))

    assert resp.status_code == 403
    assert _messages(sync) == []
    assert _runs(sync) == []
    assert upstreams.dispatched == []


@pytest.mark.parametrize("message", [
    {"content": 5},
    {"attachment_ids": ["att_1"]},
    {"content": "x", "attachment_ids": "att_1"},
    {"content": "x", "role": "ASSISTANT"},
    "hello",
])
def test_a_malformed_message_is_refused_before_anything_happens(
    client: TestClient, db: Any, upstreams: _Upstreams, auth_headers: dict[str, str],
    message: Any,
) -> None:
    _, sync = db
    _seed(sync)

    resp = client.post(URL, headers=auth_headers, json={"task": "x", "message": message})

    assert resp.status_code == 422
    assert _messages(sync) == []
    assert _runs(sync) == []
    assert upstreams.dispatched == []


def test_a_run_without_a_message_writes_none(
    client: TestClient, db: Any, upstreams: _Upstreams, auth_headers: dict[str, str],
) -> None:
    """A retry re-runs the turn already in the transcript; App runs have none."""
    _, sync = db
    _seed(sync, ("USER", "redo this"))

    resp = client.post(URL, headers=auth_headers, json={"task": "redo this"})

    assert resp.status_code == 201, resp.text
    assert "message" not in resp.json()
    assert _messages(sync) == [("USER", "redo this")]
    (dispatch,) = upstreams.dispatched
    assert dispatch["history"] == []
