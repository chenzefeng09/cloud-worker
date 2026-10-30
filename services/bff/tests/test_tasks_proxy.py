"""Tests for the BFF task board endpoints (``/tasks``).

A small in-memory fake of the orchestrator stands in for the upstream: it
keeps sessions, messages and runs so create → retry flows can be asserted
end to end (what was persisted, rewound and forwarded to ``POST /runs``).
Like the orchestrator, it writes a turn's user message only together with
its run: a refused run leaves no message.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient

_BASE = "/api/bff/v1"
_ORCH = "http://orch-test:8006/api/session/v1"


class FakeOrchestrator:
    def __init__(self) -> None:
        self.sessions: dict[str, dict[str, Any]] = {}
        self.messages: dict[str, list[dict[str, Any]]] = {}
        self.runs: dict[str, list[dict[str, Any]]] = {}
        self.calls: list[tuple[str, str, Any]] = []
        self.run_status_code = 201
        self.run_bodies: list[dict[str, Any]] = []

    # ── helpers ──────────────────────────────────────────────────────────
    def seed_session(self, sid: str, *, kind: str | None = "task",
                     task: dict | None = None) -> None:
        meta: dict[str, Any] = {}
        if kind:
            meta["kind"] = kind
        if task:
            meta["task"] = task
        self.sessions[sid] = {"id": sid, "title": "New task", "metadata": meta}
        self.messages.setdefault(sid, [])
        self.runs.setdefault(sid, [])

    def add_message(self, sid: str, role: str, content: str,
                    attachments: list[dict] | None = None) -> None:
        msgs = self.messages[sid]
        msgs.append({"id": f"msg_{len(msgs) + 1}", "role": role, "content": content,
                     "sequence": len(msgs) + 1, "attachments": attachments or []})

    def task_view(self, sid: str) -> dict[str, Any]:
        s = self.sessions[sid]
        runs = self.runs[sid]
        return {"id": sid, "title": s["title"], "kind": s["metadata"].get("kind") or "chat",
                "status": "running" if runs else "idle", "run_count": len(runs)}

    # ── routing ──────────────────────────────────────────────────────────
    def handle(self, method: str, url: str, **kw: Any) -> httpx.Response:
        path = url.removeprefix(_ORCH)
        self.calls.append((method, path, kw.get("json") or kw.get("params")))
        parts = path.strip("/").split("/")

        if method == "POST" and path == "/sessions":
            sid = f"ses_{len(self.sessions) + 1}"
            body = kw["json"]
            self.seed_session(sid, kind=(body.get("agent_config") or {}).get("kind"))
            self.sessions[sid]["title"] = body.get("title")
            self.sessions[sid]["workspace_id"] = body.get("workspace_id")
            return httpx.Response(201, json=self.sessions[sid])
        if parts[0] == "tasks" and method == "GET":
            sid = parts[1]
            if sid not in self.sessions:
                return httpx.Response(404, json={"detail": "nope"})
            return httpx.Response(200, json=self.task_view(sid))
        if parts[0] == "sessions" and len(parts) >= 2:
            sid = parts[1]
            if sid not in self.sessions:
                return httpx.Response(404, json={"detail": "nope"})
            rest = parts[2:]
            if not rest and method == "GET":
                return httpx.Response(200, json=self.sessions[sid])
            if not rest and method == "PATCH":
                body = kw["json"]
                if "task" in body:
                    self.sessions[sid]["metadata"]["task"] = body["task"]
                if body.get("title"):
                    self.sessions[sid]["title"] = body["title"]
                return httpx.Response(200, json=self.sessions[sid])
            if rest == ["messages"] and method == "GET":
                return httpx.Response(200, json={"items": self.messages[sid], "next_cursor": None})
            if rest == ["messages"] and method == "DELETE":
                frm = int(kw["params"]["from_sequence"])
                self.messages[sid] = [m for m in self.messages[sid] if m["sequence"] < frm]
                return httpx.Response(200, json={"deleted_count": 1})
            if rest == ["attachments"] and method == "GET":
                return httpx.Response(200, json={"items": [
                    {"id": "att_1", "blob_key": "k/att_1", "filename": "a.pdf", "mime": "x"},
                ]})
            if rest == ["runs"] and method == "GET":
                return httpx.Response(200, json={"items": list(reversed(self.runs[sid]))})
            if rest == ["runs"] and method == "POST":
                body = kw["json"]
                self.run_bodies.append(body)
                if self.run_status_code >= 400:
                    return httpx.Response(self.run_status_code, json={"detail": "down"})
                run = {"id": f"run_{len(self.runs[sid]) + 1}", "status": "RUNNING",
                       "task_description": body["task"]}
                self.runs[sid].append(run)
                created = dict(run)
                if body.get("message") is not None:
                    turn = body["message"]
                    self.add_message(sid, "USER", turn["content"],
                                     [{"id": a} for a in turn.get("attachment_ids") or []])
                    created["message"] = self.messages[sid][-1]
                return httpx.Response(201, json=created)
        return httpx.Response(500, json={"detail": f"unrouted {method} {path}"})


@pytest.fixture
def orch(monkeypatch: pytest.MonkeyPatch) -> FakeOrchestrator:
    fake = FakeOrchestrator()

    class _AC:
        def __init__(self, *a: Any, **kw: Any) -> None:
            pass

        async def __aenter__(self) -> _AC:
            return self

        async def __aexit__(self, *a: Any) -> None:
            pass

        async def get(self, url: str, **kw: Any) -> httpx.Response:
            return fake.handle("GET", url, **kw)

        async def post(self, url: str, **kw: Any) -> httpx.Response:
            return fake.handle("POST", url, **kw)

        async def patch(self, url: str, **kw: Any) -> httpx.Response:
            return fake.handle("PATCH", url, **kw)

        async def delete(self, url: str, **kw: Any) -> httpx.Response:
            return fake.handle("DELETE", url, **kw)

    monkeypatch.setattr(httpx, "AsyncClient", _AC)
    # Title generation calls the LLM gateway; keep it out of these tests.
    from agent_bff.api.v1 import conversations

    monkeypatch.setattr(conversations, "generate_and_update_session_title", AsyncMock())
    return fake


# ── list / get ───────────────────────────────────────────────────────────────


def test_tasks_require_auth(client: TestClient) -> None:
    assert client.get(f"{_BASE}/tasks").status_code == 401
    assert client.post(f"{_BASE}/tasks", json={"instructions": "x"}).status_code == 401


def test_list_tasks_relays_only_known_filters(
    client: TestClient, authed_cookie: dict[str, str], orch: FakeOrchestrator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def _handle(method: str, url: str, **kw: Any) -> httpx.Response:
        captured["url"], captured["params"] = url, kw.get("params")
        return httpx.Response(200, json={"items": [], "next_cursor": None, "counts": {}})

    monkeypatch.setattr(orch, "handle", _handle)
    resp = client.get(f"{_BASE}/tasks?status=running,failed&q=report&evil=1",
                      cookies=authed_cookie)
    assert resp.status_code == 200
    assert captured["url"] == f"{_ORCH}/tasks"
    assert captured["params"] == {"status": "running,failed", "q": "report"}


# ── create ───────────────────────────────────────────────────────────────────


def test_create_task_creates_session_spec_message_and_run(
    client: TestClient, authed_cookie: dict[str, str], orch: FakeOrchestrator,
) -> None:
    resp = client.post(f"{_BASE}/tasks", cookies=authed_cookie, json={
        "instructions": "  Compile a competitor report  ",
        "workspace_id": "wsp_1",
        "skills": [],
        "model": "claude-sonnet-5-5",
        "hitl": False,
    })
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["run_id"] == "run_1"
    assert body["task"]["kind"] == "task"
    sid = body["task"]["id"]
    session = orch.sessions[sid]
    assert session["workspace_id"] == "wsp_1"
    spec = session["metadata"]["task"]
    assert spec["instructions"] == "Compile a competitor report"
    assert spec["options"] == {"skills": [], "model": "claude-sonnet-5-5", "hitl": False}
    assert spec["attachment_ids"] == []
    assert orch.messages[sid][0]["content"] == "Compile a competitor report"
    run_body = orch.run_bodies[0]
    assert run_body["task"] == "Compile a competitor report"
    # The instructions are written as the first user turn with the run.
    assert run_body["message"] == {"content": "Compile a competitor report", "attachment_ids": []}
    assert run_body["skills"] == []
    assert "mcp_server_ids" not in run_body  # omitted → tenant defaults
    assert run_body["hitl"] is False
    assert run_body["model"] == "claude-sonnet-5-5"


def test_create_task_defaults_keep_hitl_on_and_skills_unset(
    client: TestClient, authed_cookie: dict[str, str], orch: FakeOrchestrator,
) -> None:
    resp = client.post(f"{_BASE}/tasks", cookies=authed_cookie,
                       json={"instructions": "Do it", "title": "My task"})
    assert resp.status_code == 201, resp.text
    run_body = orch.run_bodies[0]
    assert "hitl" not in run_body  # orchestrator default (on)
    assert "skills" not in run_body
    sid = resp.json()["task"]["id"]
    assert orch.sessions[sid]["title"] == "My task"


def test_create_task_rejects_blank_instructions(
    client: TestClient, authed_cookie: dict[str, str], orch: FakeOrchestrator,
) -> None:
    resp = client.post(f"{_BASE}/tasks", cookies=authed_cookie, json={"instructions": "   "})
    assert resp.status_code == 422
    assert not orch.sessions


def test_create_task_adopts_draft_session_with_attachments(
    client: TestClient, authed_cookie: dict[str, str], orch: FakeOrchestrator,
) -> None:
    orch.seed_session("ses_draft")
    resp = client.post(f"{_BASE}/tasks", cookies=authed_cookie, json={
        "instructions": "Summarise the attached PDF",
        "session_id": "ses_draft",
        "attachment_ids": ["att_1"],
    })
    assert resp.status_code == 201, resp.text
    assert resp.json()["task"]["id"] == "ses_draft"
    assert len(orch.sessions) == 1
    assert orch.run_bodies[0]["files"] == [
        {"key": "k/att_1", "filename": "a.pdf", "mime": "x"},
    ]


@pytest.mark.parametrize(("kind", "runs"), [(None, 0), ("task", 1)])
def test_create_task_refuses_non_draft_sessions(
    client: TestClient, authed_cookie: dict[str, str], orch: FakeOrchestrator,
    kind: str | None, runs: int,
) -> None:
    orch.seed_session("ses_x", kind=kind)
    orch.runs["ses_x"] = [{"id": "run_0", "status": "COMPLETED"}] * runs
    resp = client.post(f"{_BASE}/tasks", cookies=authed_cookie,
                       json={"instructions": "x", "session_id": "ses_x"})
    assert resp.status_code == 409
    assert not orch.run_bodies


def test_create_task_unknown_draft_is_404(
    client: TestClient, authed_cookie: dict[str, str], orch: FakeOrchestrator,
) -> None:
    resp = client.post(f"{_BASE}/tasks", cookies=authed_cookie,
                       json={"instructions": "x", "session_id": "ses_someone_elses"})
    assert resp.status_code == 404


def test_create_task_run_failure_reports_task_id(
    client: TestClient, authed_cookie: dict[str, str], orch: FakeOrchestrator,
) -> None:
    orch.run_status_code = 503
    resp = client.post(f"{_BASE}/tasks", cookies=authed_cookie, json={"instructions": "x"})
    assert resp.status_code == 503
    body = resp.json()
    assert body["task_id"] in orch.sessions
    assert body["reason"] == "task_not_started"
    assert "could not be started" in body["detail"]
    # The task is defined, but no unanswered instructions sit in its transcript.
    assert orch.messages[body["task_id"]] == []


def test_task_that_never_started_retries_with_its_attachments(
    client: TestClient, authed_cookie: dict[str, str], orch: FakeOrchestrator,
) -> None:
    orch.seed_session("ses_draft")
    orch.run_status_code = 503
    resp = client.post(f"{_BASE}/tasks", cookies=authed_cookie, json={
        "instructions": "Summarise the attached PDF",
        "session_id": "ses_draft",
        "attachment_ids": ["att_1"],
    })
    assert resp.status_code == 503
    assert orch.sessions["ses_draft"]["metadata"]["task"]["attachment_ids"] == ["att_1"]

    orch.run_status_code = 201
    resp = client.post(f"{_BASE}/tasks/ses_draft/retry", cookies=authed_cookie)
    assert resp.status_code == 201, resp.text
    run_body = orch.run_bodies[-1]
    assert run_body["message"] == {
        "content": "Summarise the attached PDF", "attachment_ids": ["att_1"],
    }
    assert run_body["files"] == [{"key": "k/att_1", "filename": "a.pdf", "mime": "x"}]
    assert [m["content"] for m in orch.messages["ses_draft"]] == ["Summarise the attached PDF"]


# ── retry ────────────────────────────────────────────────────────────────────


def test_retry_rewinds_failed_attempt_and_replays_options(
    client: TestClient, authed_cookie: dict[str, str], orch: FakeOrchestrator,
) -> None:
    orch.seed_session("ses_r", task={"instructions": "Build it",
                                     "options": {"skills": ["pdf"], "hitl": False}})
    orch.add_message("ses_r", "USER", "Build it", [{"id": "att_1"}])
    orch.add_message("ses_r", "assistant", "partial…")
    orch.add_message("ses_r", "system", "Task failed: boom")
    orch.runs["ses_r"] = [{"id": "run_1", "status": "FAILED", "task_description": "Build it"}]

    resp = client.post(f"{_BASE}/tasks/ses_r/retry", cookies=authed_cookie)
    assert resp.status_code == 201, resp.text
    # The failed attempt's output is gone; the user turn is kept, not duplicated.
    assert [m["content"] for m in orch.messages["ses_r"]] == ["Build it"]
    # The open transcript is told where the cleared rows started.
    assert resp.json()["rewound_from"] == 2
    run_body = orch.run_bodies[-1]
    assert run_body["task"] == "Build it"
    assert run_body["skills"] == ["pdf"]
    assert run_body["hitl"] is False
    assert run_body["files"][0]["key"] == "k/att_1"
    # The kept user turn is re-run, not written again.
    assert "message" not in run_body


def test_retry_overrides_take_precedence(
    client: TestClient, authed_cookie: dict[str, str], orch: FakeOrchestrator,
) -> None:
    orch.seed_session("ses_o", task={"instructions": "Go", "options": {"skills": ["a"]}})
    orch.add_message("ses_o", "USER", "Go")
    orch.runs["ses_o"] = [{"id": "run_1", "status": "CANCELLED", "task_description": "Go"}]
    resp = client.post(f"{_BASE}/tasks/ses_o/retry", cookies=authed_cookie,
                       json={"skills": [], "model": "m2"})
    assert resp.status_code == 201, resp.text
    assert orch.run_bodies[-1]["skills"] == []
    assert orch.run_bodies[-1]["model"] == "m2"


def test_retry_refuses_active_task(
    client: TestClient, authed_cookie: dict[str, str], orch: FakeOrchestrator,
) -> None:
    orch.seed_session("ses_a")
    orch.add_message("ses_a", "USER", "Go")
    orch.runs["ses_a"] = [{"id": "run_1", "status": "PAUSED", "task_description": "Go"}]
    resp = client.post(f"{_BASE}/tasks/ses_a/retry", cookies=authed_cookie)
    assert resp.status_code == 409
    assert not orch.run_bodies


def test_retry_of_never_started_task_uses_stored_instructions(
    client: TestClient, authed_cookie: dict[str, str], orch: FakeOrchestrator,
) -> None:
    """The first run failed to launch, so no message was saved: write it with this run."""
    orch.seed_session("ses_n", task={"instructions": "Start me", "options": {}})
    resp = client.post(f"{_BASE}/tasks/ses_n/retry", cookies=authed_cookie)
    assert resp.status_code == 201, resp.text
    assert [m["content"] for m in orch.messages["ses_n"]] == ["Start me"]
    assert orch.run_bodies[-1]["task"] == "Start me"


def test_retry_of_chat_session_reruns_last_turn(
    client: TestClient, authed_cookie: dict[str, str], orch: FakeOrchestrator,
) -> None:
    orch.seed_session("ses_c", kind=None)
    orch.add_message("ses_c", "USER", "first")
    orch.add_message("ses_c", "assistant", "answer")
    orch.add_message("ses_c", "USER", "second")
    orch.runs["ses_c"] = [
        {"id": "run_1", "status": "COMPLETED", "task_description": "first"},
        {"id": "run_2", "status": "FAILED", "task_description": "second"},
    ]
    resp = client.post(f"{_BASE}/tasks/ses_c/retry", cookies=authed_cookie)
    assert resp.status_code == 201, resp.text
    assert orch.run_bodies[-1]["task"] == "second"
    assert [m["content"] for m in orch.messages["ses_c"]] == ["first", "answer", "second"]


def test_retry_unknown_task_is_404(
    client: TestClient, authed_cookie: dict[str, str], orch: FakeOrchestrator,
) -> None:
    resp = client.post(f"{_BASE}/tasks/ses_missing/retry", cookies=authed_cookie)
    assert resp.status_code == 404
