"""GDPR hooks in the existing session paths.

* session delete → best-effort knowledge-ingest memory purge
* inactivity sweep keys on ``updated_at`` and propagates the system actor
* conversation rewind deletes the orphaned output blobs (reference-aware)
* run completion: memory write gated by the opt-out; writeback eviction goes
  through the reference-aware blob helper
* run creation: memory prefetch gated by the opt-out
* memory / knowledge search use ``POST /search`` with a JSON body
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from agent_session_orchestrator.config import Settings
from agent_session_orchestrator.domain.models import (
    Attachment,
    Message,
    Run,
    Session,
    Workspace,
    WorkspaceFile,
)
from agent_session_orchestrator.services import knowledge_client as kc
from agent_session_orchestrator.services import privacy_service as ps
from agent_session_orchestrator.services import session_service as ss
from tests._helpers import TEST_TENANT, TEST_USER, AuthzAwareClient
from tests._privacy_harness import FakeBlobStore, RecordingHttpClient, new_db, ts

KI = "http://ki:8012/api/knowledge/v1"


def _run(coro: Any) -> Any:
    return asyncio.get_event_loop().run_until_complete(coro)


def _count(sync: Any, model: Any, **where: Any) -> int:
    stmt = select(func.count()).select_from(model)
    for key, value in where.items():
        stmt = stmt.where(getattr(model, key) == value)
    return int(sync.execute(stmt).scalar_one())


# ── session delete → memory purge ────────────────────────────────────────────


def _seed_session(sync: Any, session_id: str = "ses_1", **kw: Any) -> None:
    sync.add(Session(id=session_id, tenant_id=TEST_TENANT, user_id=TEST_USER,
                     status="ACTIVE", **kw))
    sync.commit()


def test_delete_session_purges_memories_as_owner() -> None:
    db, sync = new_db()
    _seed_session(sync)
    http = RecordingHttpClient()
    with patch.object(ps.httpx, "AsyncClient", http):
        _run(ss.SessionService(db, tenant_id=TEST_TENANT).delete_session(
            "ses_1", knowledge_ingest_url=KI,
        ))
    assert sync.get(Session, "ses_1").deleted_at is not None
    ((method, url, kw),) = http.calls
    assert (method, url) == ("DELETE", f"{KI}/memories")
    assert kw["params"] == {"origin_session_id": "ses_1"}
    assert kw["headers"] == {"X-Tenant-Id": TEST_TENANT, "X-User-Id": TEST_USER}


def test_system_delete_purges_memories_as_admin_via_registered_settings() -> None:
    db, sync = new_db()
    _seed_session(sync)
    http = RecordingHttpClient()
    settings = Settings(env="test", otel_enabled=False, knowledge_ingest_url=KI)
    with patch.object(ps.httpx, "AsyncClient", http), \
            patch.object(ps, "registered_settings", lambda: settings):
        _run(ss.SessionService(db, tenant_id=TEST_TENANT).delete_session("ses_1", as_admin=True))
    ((_, url, kw),) = http.calls
    assert url == f"{KI}/memories"
    assert kw["headers"]["X-Actor"] == "ADMIN"
    assert kw["headers"]["X-User-Id"] == TEST_USER


def test_delete_session_never_fails_on_knowledge_outage() -> None:
    db, sync = new_db()
    _seed_session(sync)
    with patch.object(ps.httpx, "AsyncClient", RecordingHttpClient(
        raise_exc=httpx.ConnectError("down"),
    )):
        _run(ss.SessionService(db, tenant_id=TEST_TENANT).delete_session(
            "ses_1", knowledge_ingest_url=KI,
        ))
    assert sync.get(Session, "ses_1").deleted_at is not None


# ── inactivity sweep ─────────────────────────────────────────────────────────


def test_inactivity_sweep_keys_on_updated_at_and_deletes_as_system() -> None:
    db, sync = new_db()
    _seed_session(sync, "ses_old")
    scans: list[tuple[str, dict[str, Any]]] = []

    async def _privileged(settings: Any, sql: str, params: dict[str, Any]) -> list[str]:
        scans.append((sql, params))
        return [f"{TEST_TENANT}\x1fses_old"]

    async def _gen(**kw: Any) -> AsyncIterator[Any]:
        assert kw == {"tenant_id": TEST_TENANT, "user_id": "SYSTEM", "actor": "SYSTEM"}
        yield db

    http = RecordingHttpClient()
    settings = Settings(env="test", otel_enabled=False, knowledge_ingest_url=KI)
    with patch.object(ss, "privileged_scalars", _privileged), \
            patch.object(ss, "get_session", _gen), \
            patch.object(ps.httpx, "AsyncClient", http):
        purged = _run(ss.purge_expired_sessions(settings, 30))
    assert purged == 1
    ((sql, params),) = scans
    assert "deleted_at IS NULL AND updated_at < :cutoff" in sql
    assert "created_at" not in sql
    assert sync.get(Session, "ses_old").deleted_at is not None
    ((_, url, kw),) = http.calls
    assert url == f"{KI}/memories" and kw["headers"]["X-Actor"] == "ADMIN"


@pytest.mark.parametrize("days", [0, -5])
def test_inactivity_sweep_disabled_when_retention_not_positive(days: int) -> None:
    scan = AsyncMock()
    with patch.object(ss, "privileged_scalars", scan):
        assert _run(ss.purge_expired_sessions(Settings(env="test"), days)) == 0
    scan.assert_not_awaited()


# ── rewind ───────────────────────────────────────────────────────────────────


def test_rewind_deletes_orphaned_output_blobs_reference_aware() -> None:
    db, sync = new_db()
    t = TEST_TENANT
    sync.add_all([
        Session(id="ses_1", tenant_id=t, user_id=TEST_USER, status="ACTIVE"),
        Workspace(id="wsp_1", tenant_id=t, user_id=TEST_USER, name="w", skills=[], meta={}),
    ])
    sync.flush()
    sync.add_all([
        Message(id="m1", tenant_id=t, session_id="ses_1", role="user", content="1",
                sequence=1, run_id="run_1"),
        Message(id="m2", tenant_id=t, session_id="ses_1", role="assistant", content="2",
                sequence=2),
        Message(id="m3", tenant_id=t, session_id="ses_1", role="user", content="3",
                sequence=3, run_id="run_3"),
        Message(id="m4", tenant_id=t, session_id="ses_1", role="assistant", content="4",
                sequence=4, run_id="run_4"),
        Attachment(id="a_o1", tenant_id=t, session_id="ses_1", user_id=TEST_USER,
                   filename="o1", blob_key="k/o1", kind="output", run_id="run_1"),
        Attachment(id="a_o3", tenant_id=t, session_id="ses_1", user_id=TEST_USER,
                   filename="o3", blob_key="k/o3", kind="output", run_id="run_3"),
        Attachment(id="a_o4", tenant_id=t, session_id="ses_1", user_id=TEST_USER,
                   filename="o4", blob_key="k/o4", kind="output", run_id="run_4"),
        Attachment(id="a_in", tenant_id=t, session_id="ses_1", user_id=TEST_USER,
                   filename="in", blob_key="k/in"),
        # The run_4 output was also written back into the workspace.
        WorkspaceFile(id="wsf_1", tenant_id=t, workspace_id="wsp_1", user_id=TEST_USER,
                      filename="o4", blob_key="k/o4"),
    ])
    sync.commit()

    blobs = FakeBlobStore()
    deleted = _run(ss.SessionService(db, tenant_id=t).delete_messages_from_sequence(
        "ses_1", 3, blob_store=blobs,
    ))
    sync.commit()
    assert deleted == 2
    assert _count(sync, Message, session_id="ses_1") == 2
    remaining = set(sync.execute(select(Attachment.id)).scalars().all())
    assert remaining == {"a_o1", "a_in"}
    assert blobs.deleted == ["k/o3"]  # k/o4 still backs the workspace file


def test_rewind_without_blob_store_still_truncates() -> None:
    db, sync = new_db()
    t = TEST_TENANT
    sync.add(Session(id="ses_1", tenant_id=t, user_id=TEST_USER, status="ACTIVE"))
    sync.flush()
    sync.add_all([
        Message(id="m1", tenant_id=t, session_id="ses_1", role="user", content="1",
                sequence=1, run_id="run_1"),
        Attachment(id="a1", tenant_id=t, session_id="ses_1", user_id=TEST_USER,
                   filename="o", blob_key="k/o", kind="output", run_id="run_1"),
    ])
    sync.commit()
    with patch.object(ps, "registered_blob_store", lambda: None):
        assert _run(ss.SessionService(db, tenant_id=t).delete_messages_from_sequence(
            "ses_1", 1,
        )) == 1
    assert _count(sync, Attachment) == 0


# ── run completion: memory gate + writeback eviction ─────────────────────────


def _running_run() -> Run:
    return Run(
        id="run_1", tenant_id=TEST_TENANT, session_id="ses_1", status="RUNNING",
        sandbox_id="ctr_1", task_description="hello", created_at=ts(0), started_at=ts(0),
    )


class _SchedulerClient:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    async def __aenter__(self) -> _SchedulerClient:
        return self

    async def __aexit__(self, *a: Any) -> None:
        return None

    async def get(self, url: str, **kw: Any) -> MagicMock:
        resp = MagicMock()
        resp.status_code = 200
        resp.json = MagicMock(return_value=self.payload)
        return resp


_COMPLETED = {
    "status": "COMPLETED", "result_summary": "done", "result_error": None,
    "artifacts": [], "usage": {}, "plan_steps": [],
}


@pytest.mark.parametrize("enabled", [True, False])
def test_run_completion_memory_write_honours_opt_out(enabled: bool) -> None:
    from agent_session_orchestrator.api.v1 import messages

    svc = AsyncMock()
    gate = AsyncMock(return_value=enabled)
    write = AsyncMock()
    with patch.object(httpx, "AsyncClient", return_value=_SchedulerClient(_COMPLETED)), \
            patch.object(ps, "memory_enabled_for", gate), \
            patch.object(messages, "_write_memory", write):
        terminal, _ = _run(messages._reconcile_run_completion(
            scheduler_url="http://sched", tenant_id=TEST_TENANT, user_id=TEST_USER,
            run=_running_run(), svc=svc, knowledge_ingest_url=KI,
        ))
    assert terminal is True
    gate.assert_awaited_once()
    assert gate.call_args.kwargs == {"tenant_id": TEST_TENANT, "user_id": TEST_USER}
    assert write.await_count == (1 if enabled else 0)


def test_run_completion_writeback_eviction_is_reference_aware() -> None:
    from agent_session_orchestrator.api.v1 import messages
    from agent_session_orchestrator.services.platform_config_service import (
        PlatformConfigService,
    )
    from agent_session_orchestrator.services.workspace_service import WorkspaceService

    payload = {
        **_COMPLETED,
        "writeback_files": [{"filename": "a.md", "blob_key": "k/new", "size_bytes": 3}],
    }
    svc = AsyncMock()
    svc.get_session.return_value = SimpleNamespace(workspace_id="wsp_1", user_id=TEST_USER)
    writeback = AsyncMock(return_value=[{
        "file_id": "wsf_1", "filename": "a.md", "version": 3, "is_new": False,
        "evicted_blobs": ["k/v1", "k/v2"],
    }])
    evict = AsyncMock(return_value=1)
    with patch.object(httpx, "AsyncClient", return_value=_SchedulerClient(payload)), \
            patch.object(WorkspaceService, "process_writeback", writeback), \
            patch.object(PlatformConfigService, "get", AsyncMock(return_value=20)), \
            patch.object(ps, "delete_unreferenced_blobs", evict):
        _run(messages._reconcile_run_completion(
            scheduler_url="http://sched", tenant_id=TEST_TENANT, user_id=TEST_USER,
            run=_running_run(), svc=svc,
        ))
    writeback.assert_awaited_once()
    evict.assert_awaited_once()
    assert evict.call_args.args == (svc._db,)
    assert evict.call_args.kwargs == {"tenant_id": TEST_TENANT, "keys": ["k/v1", "k/v2"]}


# ── run creation: memory prefetch gate + POST /search ────────────────────────


class _RunClient(AuthzAwareClient):
    def __init__(self) -> None:
        self.searches: list[dict[str, Any]] = []
        self.scheduler_bodies: list[dict[str, Any]] = []

    async def post(self, url: str, **kw: Any) -> httpx.Response:
        if "/check" in url and "authz" in url:
            return self._authz_allowed_resp()
        if url.endswith("/search"):
            body = kw.get("json") or {}
            assert "params" not in kw  # the prompt never rides in the URL
            self.searches.append({**body, "_headers": kw.get("headers") or {}})
            hits = [{"text": "user is called Zhang San"}] if body.get("scope") == "memory" else []
            return httpx.Response(200, json={"hits": hits})
        if kw.get("json") is not None:
            self.scheduler_bodies.append(kw["json"])
        return httpx.Response(201, json={"id": "run_mem", "sandbox_id": "abc", "status": "RUNNING"})

    async def get(self, url: str, **kw: Any) -> httpx.Response:
        return httpx.Response(200, json={"items": []})


def _create_run(app_client: TestClient, mock_db: AsyncMock, auth_headers: dict[str, str],
                enabled: bool) -> tuple[_RunClient, AsyncMock]:
    import datetime as dt

    from tests._helpers import make_scalar_result

    session = Session(id="ses_mem", tenant_id=TEST_TENANT, user_id=TEST_USER, status="ACTIVE")
    mock_db.execute.return_value = make_scalar_result(session)

    async def _refresh(obj: Any) -> None:
        if isinstance(obj, Run):
            obj.id = "run_mem"
            obj.created_at = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)

    mock_db.refresh.side_effect = _refresh
    client = _RunClient()
    gate = AsyncMock(return_value=enabled)
    with patch.object(httpx, "AsyncClient", return_value=client), \
            patch.object(ps, "memory_enabled_for", gate):
        app_client.post(
            "/api/session/v1/sessions/ses_mem/runs", headers=auth_headers,
            json={"task": "what is my name"},
        )
    return client, gate


def test_create_run_prefetches_memory_via_post_when_enabled(
    app_client: TestClient, mock_db: AsyncMock, auth_headers: dict[str, str],
) -> None:
    client, gate = _create_run(app_client, mock_db, auth_headers, enabled=True)
    gate.assert_awaited_once()
    assert gate.call_args.kwargs == {"tenant_id": TEST_TENANT, "user_id": TEST_USER}
    memory = [s for s in client.searches if s.get("scope") == "memory"]
    assert len(memory) == 1
    assert memory[0]["q"] == "what is my name"
    assert memory[0]["exclude_origin_session_id"] == "ses_mem"
    assert memory[0]["_headers"]["X-User-Id"] == TEST_USER
    assert "Zhang San" in client.scheduler_bodies[-1]["memory"]


def test_create_run_skips_memory_prefetch_when_opted_out(
    app_client: TestClient, mock_db: AsyncMock, auth_headers: dict[str, str],
) -> None:
    client, gate = _create_run(app_client, mock_db, auth_headers, enabled=False)
    gate.assert_awaited_once()
    assert [s for s in client.searches if s.get("scope") == "memory"] == []
    assert client.scheduler_bodies[-1]["memory"] == ""


# ── search contracts ─────────────────────────────────────────────────────────


def test_fetch_memory_posts_json_body() -> None:
    from agent_session_orchestrator.api.v1.runs import _fetch_memory

    http = RecordingHttpClient()

    async def _post(url: str, **kw: Any) -> httpx.Response:
        http.calls.append(("POST", url, kw))
        return httpx.Response(200, json={"hits": [{"text": "likes short answers"}, {"text": ""}]})

    http.post = _post  # type: ignore[method-assign]
    with patch.object(httpx, "AsyncClient", http):
        out = _run(_fetch_memory(
            knowledge_ingest_url=KI, tenant_id=TEST_TENANT, user_id=TEST_USER,
            query="x" * 600, session_id="ses_1", clearance="L3",
        ))
        _run(_fetch_memory(
            knowledge_ingest_url=KI, tenant_id=TEST_TENANT, user_id=TEST_USER, query="hi",
        ))
    assert out == "- likes short answers"
    (_, url, kw), (_, _, kw2) = http.calls
    assert url == f"{KI}/search"
    assert "params" not in kw
    assert kw["json"] == {
        "q": "x" * 500, "k": kw["json"]["k"], "scope": "memory",
        "exclude_origin_session_id": "ses_1",
    }
    assert kw["headers"] == {
        "X-Tenant-Id": TEST_TENANT, "X-User-Id": TEST_USER, "X-User-Clearance": "L3",
    }
    assert "exclude_origin_session_id" not in kw2["json"]


def test_fetch_memory_failure_open_on_post_error() -> None:
    from agent_session_orchestrator.api.v1.runs import _fetch_memory

    with patch.object(httpx, "AsyncClient", RecordingHttpClient(
        raise_exc=httpx.ConnectError("down"),
    )):
        assert _run(_fetch_memory(
            knowledge_ingest_url=KI, tenant_id=TEST_TENANT, user_id=TEST_USER, query="hi",
        )) == ""


def test_knowledge_prefetch_posts_json_body() -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    class _Client:
        async def __aenter__(self) -> _Client:
            return self

        async def __aexit__(self, *a: Any) -> None:
            return None

        async def post(self, url: str, **kw: Any) -> httpx.Response:
            calls.append((url, kw))
            return httpx.Response(200, json={"hits": [
                {"document_name": "Handbook", "folder_path": "Ops", "text": "step one"},
            ]})

    with patch.object(httpx, "AsyncClient", return_value=_Client()):
        block = _run(kc.fetch_knowledge_prefetch(
            base_url=KI, tenant_id=TEST_TENANT, user_id=TEST_USER, clearance="L2",
            query="how", k=3,
        ))
    assert block == "[Handbook | Ops]\nstep one"
    ((url, kw),) = calls
    assert url == f"{KI}/search"
    assert "params" not in kw
    assert kw["json"] == {"q": "how", "k": 3, "scope": "knowledge"}
    assert kw["headers"]["X-User-Clearance"] == "L2"


def test_knowledge_prefetch_failure_open() -> None:
    with patch.object(httpx, "AsyncClient", RecordingHttpClient(status=500)):
        assert _run(kc.fetch_knowledge_prefetch(
            base_url=KI, tenant_id=TEST_TENANT, user_id=TEST_USER, clearance="L2", query="q",
        )) == ""


# ── workspace file delete ────────────────────────────────────────────────────


def test_deleting_a_file_with_versions_keeps_bytes_another_row_uses() -> None:
    from agent_session_orchestrator.domain.models import WorkspaceFileVersion
    from agent_session_orchestrator.services.workspace_service import WorkspaceService

    db, sync = new_db()
    t = TEST_TENANT
    sync.add(Workspace(id="wsp_1", tenant_id=t, user_id=TEST_USER, name="w", skills=[], meta={}))
    sync.flush()
    sync.add_all([
        WorkspaceFile(id="wsf_1", tenant_id=t, workspace_id="wsp_1", user_id=TEST_USER,
                      filename="a.md", blob_key="k/a3"),
        # A copy of a.md's first version lives on as another file.
        WorkspaceFile(id="wsf_2", tenant_id=t, workspace_id="wsp_1", user_id=TEST_USER,
                      filename="copy.md", blob_key="k/a1"),
    ])
    sync.flush()
    sync.add_all([
        WorkspaceFileVersion(tenant_id=t, workspace_file_id="wsf_1", version=1,
                             blob_key="k/a1", size_bytes=1),
        WorkspaceFileVersion(tenant_id=t, workspace_file_id="wsf_1", version=2,
                             blob_key="k/a2", size_bytes=1),
    ])
    sync.commit()

    keys = _run(WorkspaceService(db, tenant_id=t).delete_file("wsp_1", "wsf_1", user_id=None))
    blobs = FakeBlobStore()
    _run(ps.delete_unreferenced_blobs(db, tenant_id=t, keys=keys, blob_store=blobs))
    sync.commit()

    assert sorted(keys) == ["k/a1", "k/a2", "k/a3"]
    assert _count(sync, WorkspaceFileVersion) == 0
    assert _count(sync, WorkspaceFile) == 1
    assert sorted(blobs.deleted) == ["k/a2", "k/a3"]  # k/a1 still backs copy.md
