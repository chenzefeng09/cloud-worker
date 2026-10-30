"""Privacy service — export, erasure, memory opt-out gate, blob hygiene, trash purge.

Runs the real service code against the SQLite harness in
``tests/_privacy_harness.py`` (FK enforcement on, RETURNING supported), so a
purge that deletes in the wrong order or forgets a child table fails here the
way it would on Postgres.  Postgres-only statements are compiled against the
PostgreSQL dialect and asserted on.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import func, select
from sqlalchemy.dialects import postgresql

from agent_session_orchestrator.config import Settings
from agent_session_orchestrator.domain.models import (
    Attachment,
    CanvasDocument,
    CanvasSuggestion,
    CanvasVersion,
    Message,
    Run,
    Session,
    UserPrivacySettings,
    Workspace,
    WorkspaceFile,
    WorkspaceFileVersion,
)
from agent_session_orchestrator.services import privacy_service as ps
from tests._helpers import TEST_TENANT, TEST_USER
from tests._privacy_harness import FakeBlobStore, RecordingHttpClient, new_db, ts

OTHER_USER = "usr_01J5TEST0000000000000USER2"
CRON_USER = "usr_01SVCCRON00000000000000000"
SCHED = "http://sched:8007/api/scheduler/v1"
KI = "http://ki:8012/api/knowledge/v1"

ALL_MODELS = (
    Session, Message, Run, Attachment, CanvasDocument, CanvasVersion, CanvasSuggestion,
    Workspace, WorkspaceFile, WorkspaceFileVersion, UserPrivacySettings,
)


def _run(coro: Any) -> Any:
    return asyncio.get_event_loop().run_until_complete(coro)


def _settings(**kw: Any) -> Settings:
    base: dict[str, Any] = {
        "env": "test",
        "otel_enabled": False,
        "agent_scheduler_url": SCHED,
        "knowledge_ingest_url": KI,
        "redis_url": "redis://redis:6379/0",
    }
    base.update(kw)
    return Settings(**base)


def _count(sync: Any, model: Any, **where: Any) -> int:
    stmt = select(func.count()).select_from(model)
    for key, value in where.items():
        stmt = stmt.where(getattr(model, key) == value)
    return int(sync.execute(stmt).scalar_one())


def _seed(sync: Any) -> None:
    """Subject (TEST_USER) + another user's data in the same tenant.

    Blob-key sharing mirrors production: an output attachment and the
    workspace file written back by the same run share ``k/out-a2``; a restore
    left ``k/ws-1-v3`` on both the current file and a version; the other
    user's attachment shares ``k/shared`` with one of the subject's.
    """
    t = TEST_TENANT
    sync.add_all([
        Session(id="ses_A", tenant_id=t, user_id=TEST_USER, status="ACTIVE", title="live",
                agent_metadata={"kind": "task"}),
        Session(id="ses_B", tenant_id=t, user_id=TEST_USER, status="ACTIVE", title="trashed",
                deleted_at=ts(3)),
        Session(id="ses_O", tenant_id=t, user_id=OTHER_USER, status="ACTIVE"),
        Workspace(id="wsp_S1", tenant_id=t, user_id=TEST_USER, name="Default",
                  is_default=True, skills=[], meta={}),
        Workspace(id="wsp_S2", tenant_id=t, user_id=TEST_USER, name="Old", skills=[],
                  meta={}, deleted_at=ts(2)),
        Workspace(id="wsp_O", tenant_id=t, user_id=OTHER_USER, name="Theirs", skills=[],
                  meta={}),
    ])
    sync.flush()
    sync.add_all([
        Message(id="msg_A1", tenant_id=t, session_id="ses_A", role="user", content="q1",
                sequence=1),
        Message(id="msg_A2", tenant_id=t, session_id="ses_A", role="assistant", content="a1",
                sequence=2, run_id="run_A1"),
        Message(id="msg_A3", tenant_id=t, session_id="ses_A", role="user", content="q2",
                sequence=3),
        Message(id="msg_B1", tenant_id=t, session_id="ses_B", role="user", content="old",
                sequence=1),
        Message(id="msg_O1", tenant_id=t, session_id="ses_O", role="user", content="theirs",
                sequence=1),
        Run(id="run_A1", tenant_id=t, session_id="ses_A", status="COMPLETED",
            task_description="q1", result_summary="a1"),
        Run(id="run_A2", tenant_id=t, session_id="ses_A", status="RUNNING",
            sandbox_id="ctr_a2", task_description="q2"),
        Run(id="run_A3", tenant_id=t, session_id="ses_A", status="PENDING",
            task_description="legacy", execution_mode="inline"),
        Run(id="run_B1", tenant_id=t, session_id="ses_B", status="PAUSED",
            sandbox_id="ctr_b1", task_description="old"),
        Run(id="run_O1", tenant_id=t, session_id="ses_O", status="RUNNING",
            sandbox_id="ctr_o1", task_description="theirs"),
        Attachment(id="att_A1", tenant_id=t, session_id="ses_A", user_id=TEST_USER,
                   filename="in.pdf", blob_key="k/in-a1", size_bytes=10),
        Attachment(id="att_A2", tenant_id=t, session_id="ses_A", user_id=TEST_USER,
                   filename="out.csv", blob_key="k/out-a2", kind="output", run_id="run_A1"),
        Attachment(id="att_B1", tenant_id=t, session_id="ses_B", user_id=TEST_USER,
                   filename="shared.png", blob_key="k/shared"),
        Attachment(id="att_O1", tenant_id=t, session_id="ses_O", user_id=OTHER_USER,
                   filename="shared.png", blob_key="k/shared"),
        CanvasDocument(id="cvs_A", tenant_id=t, session_id="ses_A", user_id=TEST_USER,
                       kind="document", title="Doc", current_version=2),
        CanvasDocument(id="cvs_B", tenant_id=t, session_id="ses_B", user_id=TEST_USER,
                       kind="document", title="Old doc", deleted_at=ts(3)),
        WorkspaceFile(id="wsf_1", tenant_id=t, workspace_id="wsp_S1", user_id=TEST_USER,
                      filename="notes.md", blob_key="k/ws-1-v3", current_version=3),
        WorkspaceFile(id="wsf_2", tenant_id=t, workspace_id="wsp_S1", user_id=TEST_USER,
                      filename="out.csv", blob_key="k/out-a2", current_version=1),
        WorkspaceFile(id="wsf_O", tenant_id=t, workspace_id="wsp_O", user_id=OTHER_USER,
                      filename="x.md", blob_key="k/o-file"),
        UserPrivacySettings(tenant_id=t, user_id=TEST_USER, memory_enabled=False),
        UserPrivacySettings(tenant_id=t, user_id=OTHER_USER, memory_enabled=True),
    ])
    sync.flush()
    sync.add_all([
        CanvasVersion(id="cvv_A1", tenant_id=t, canvas_id="cvs_A", version=1,
                      blocks=[{"id": "b1", "type": "p", "text": "v1"}], created_by="model"),
        CanvasVersion(id="cvv_A2", tenant_id=t, canvas_id="cvs_A", version=2,
                      blocks=[{"id": "b1", "type": "p", "text": "v2"}], created_by="user",
                      base_version=1),
        CanvasVersion(id="cvv_B1", tenant_id=t, canvas_id="cvs_B", version=1, blocks=[],
                      created_by="model"),
        CanvasSuggestion(id="csg_A1", tenant_id=t, canvas_id="cvs_A", base_version=2,
                         instruction="shorter", diffs=[]),
        WorkspaceFileVersion(id="wfv_1", tenant_id=t, workspace_file_id="wsf_1", version=1,
                             blob_key="k/ws-1-v1", size_bytes=1),
        WorkspaceFileVersion(id="wfv_2", tenant_id=t, workspace_file_id="wsf_1", version=2,
                             blob_key="k/ws-1-v3", size_bytes=1),
    ])
    sync.commit()


# ── memory opt-out gate ──────────────────────────────────────────────────────


def test_memory_enabled_defaults_true_without_row() -> None:
    db, _sync = new_db()
    assert _run(ps.memory_enabled_for(db, tenant_id=TEST_TENANT, user_id=TEST_USER)) is True


def test_memory_enabled_honours_opt_out_row() -> None:
    db, sync = new_db()
    sync.add(UserPrivacySettings(tenant_id=TEST_TENANT, user_id=TEST_USER, memory_enabled=False))
    sync.commit()
    assert _run(ps.memory_enabled_for(db, tenant_id=TEST_TENANT, user_id=TEST_USER)) is False
    # The row is per tenant: the same user id elsewhere keeps the default.
    assert _run(ps.memory_enabled_for(db, tenant_id="tnt_other", user_id=TEST_USER)) is True


@pytest.mark.parametrize("user_id", ["SYSTEM", "", None])
def test_memory_disabled_for_system_or_missing_user(user_id: str | None) -> None:
    db, _sync = new_db()
    assert _run(ps.memory_enabled_for(db, tenant_id=TEST_TENANT, user_id=user_id)) is False


def test_memory_lookup_error_fails_closed() -> None:
    db, _sync = new_db()

    async def _boom(*a: Any, **kw: Any) -> Any:
        raise RuntimeError("db down")

    db.execute = _boom  # type: ignore[method-assign]
    assert _run(ps.memory_enabled_for(db, tenant_id=TEST_TENANT, user_id=TEST_USER)) is False


# ── settings upsert (Postgres-only statement) ────────────────────────────────


def test_set_privacy_settings_upserts_on_tenant_user_pk() -> None:
    captured: list[Any] = []

    class _Result:
        def one(self) -> tuple[bool, dt.datetime]:
            return False, dt.datetime(2026, 10, 30, tzinfo=dt.UTC)

    class _DB:
        async def execute(self, stmt: Any) -> _Result:
            captured.append(stmt)
            return _Result()

    out = _run(ps.PrivacyService(_DB(), tenant_id=TEST_TENANT).set_privacy_settings(  # type: ignore[arg-type]
        TEST_USER, memory_enabled=False,
    ))
    assert out == {"memory_enabled": False, "updated_at": "2026-10-30T00:00:00+00:00"}
    sql = str(captured[0].compile(dialect=postgresql.dialect()))
    assert "INSERT INTO session_orchestrator.user_privacy_settings" in sql
    assert "ON CONFLICT (tenant_id, user_id) DO UPDATE" in sql
    assert "RETURNING" in sql


def test_get_privacy_settings_defaults_and_row() -> None:
    db, sync = new_db()
    svc = ps.PrivacyService(db, tenant_id=TEST_TENANT)
    assert _run(svc.get_privacy_settings(TEST_USER)) == {"memory_enabled": True, "updated_at": None}
    sync.add(UserPrivacySettings(tenant_id=TEST_TENANT, user_id=TEST_USER, memory_enabled=False))
    sync.commit()
    out = _run(svc.get_privacy_settings(TEST_USER))
    assert out["memory_enabled"] is False
    assert out["updated_at"] is not None


# ── export ───────────────────────────────────────────────────────────────────


def test_export_includes_soft_deleted_rows_and_only_the_subject() -> None:
    db, sync = new_db()
    _seed(sync)
    out = _run(ps.PrivacyService(db, tenant_id=TEST_TENANT).export_user_data(TEST_USER))

    assert out["subject_user_id"] == TEST_USER
    assert out["generated_at"]
    sessions = {s["id"]: s for s in out["sessions"]}
    assert set(sessions) == {"ses_A", "ses_B"}  # soft-deleted ses_B included, ses_O not
    assert sessions["ses_B"]["deleted_at"] is not None
    assert sessions["ses_A"]["metadata"] == {"kind": "task"}

    a = sessions["ses_A"]
    assert [m["sequence"] for m in a["messages"]] == [1, 2, 3]
    assert {r["id"] for r in a["runs"]} == {"run_A1", "run_A2", "run_A3"}
    att = {x["id"]: x for x in a["attachments"]}
    assert set(att) == {"att_A1", "att_A2"}
    assert set(att["att_A1"]) == {
        "id", "filename", "mime", "size_bytes", "kind", "created_at", "download_path",
    }
    assert att["att_A1"]["download_path"] == "/sessions/ses_A/files/att_A1/content"
    assert "blob_key" not in att["att_A2"]
    (canvas,) = a["canvases"]
    assert [v["version"] for v in canvas["versions"]] == [1, 2]
    assert canvas["versions"][1]["blocks"][0]["text"] == "v2"
    assert [s["instruction"] for s in canvas["suggestions"]] == ["shorter"]
    assert sessions["ses_B"]["canvases"][0]["deleted_at"] is not None

    workspaces = {w["id"]: w for w in out["workspaces"]}
    assert set(workspaces) == {"wsp_S1", "wsp_S2"}
    files = {f["id"]: f for f in workspaces["wsp_S1"]["files"]}
    assert set(files) == {"wsf_1", "wsf_2"}
    assert [v["version"] for v in files["wsf_1"]["versions"]] == [1, 2]
    assert files["wsf_1"]["download_path"] == "/workspaces/wsp_S1/files/wsf_1/content"
    assert out["privacy_settings"] == {"memory_enabled": False}


def test_export_for_user_without_data_is_empty() -> None:
    db, _sync = new_db()
    out = _run(ps.PrivacyService(db, tenant_id=TEST_TENANT).export_user_data(TEST_USER))
    assert out["sessions"] == [] and out["workspaces"] == []
    assert out["privacy_settings"] == {"memory_enabled": True}


# ── sessions run on the subject's behalf ─────────────────────────────────────


def _seed_on_behalf(sync: Any) -> None:
    """Scheduled-run sessions owned by the cron service account, plus decoys."""
    t = TEST_TENANT
    on_behalf = {"kind": "app_run", "on_behalf_of_user_id": TEST_USER}
    sync.add_all([
        Session(id="ses_A", tenant_id=t, user_id=TEST_USER, status="ACTIVE"),
        Session(id="ses_S1", tenant_id=t, user_id=CRON_USER, status="ACTIVE",
                agent_metadata=on_behalf),
        Session(id="ses_S2", tenant_id=t, user_id=CRON_USER, status="ACTIVE",
                agent_metadata={"kind": "app_run", "on_behalf_of_user_id": OTHER_USER}),
        Session(id="ses_U", tenant_id=t, user_id=CRON_USER, status="ACTIVE",
                agent_metadata={"kind": "app_run"}),
        # Anyone can write metadata on their own session: a tag here is no proof.
        Session(id="ses_F", tenant_id=t, user_id=OTHER_USER, status="ACTIVE",
                agent_metadata=on_behalf),
        Session(id="ses_X", tenant_id="tnt_01J5TEST00000000000000TNT2", user_id=CRON_USER,
                status="ACTIVE", agent_metadata=on_behalf),
    ])
    sync.flush()
    sync.add_all([
        Message(id="msg_S1", tenant_id=t, session_id="ses_S1", role="user",
                content="weekly digest inputs", sequence=1),
        Run(id="run_S1", tenant_id=t, session_id="ses_S1", status="RUNNING",
            sandbox_id="ctr_s1", task_description="digest", source="schedule"),
    ])
    sync.commit()


def test_export_includes_sessions_run_on_the_subjects_behalf() -> None:
    db, sync = new_db()
    _seed_on_behalf(sync)
    out = _run(ps.PrivacyService(db, tenant_id=TEST_TENANT).export_user_data(
        TEST_USER, settings=_settings(),
    ))
    sessions = {s["id"]: s for s in out["sessions"]}
    assert set(sessions) == {"ses_A", "ses_S1"}
    assert [m["content"] for m in sessions["ses_S1"]["messages"]] == ["weekly digest inputs"]
    assert sessions["ses_S1"]["user_id"] == CRON_USER


def test_on_behalf_sessions_need_a_configured_principal() -> None:
    db, sync = new_db()
    _seed_on_behalf(sync)
    out = _run(ps.PrivacyService(db, tenant_id=TEST_TENANT).export_user_data(
        TEST_USER, settings=_settings(on_behalf_principal_user_ids=[]),
    ))
    assert [s["id"] for s in out["sessions"]] == ["ses_A"]


def test_erase_includes_sessions_run_on_the_subjects_behalf() -> None:
    db, sync = new_db()
    _seed_on_behalf(sync)
    http = RecordingHttpClient()
    with patch.object(ps.httpx, "AsyncClient", http), \
            patch.object(ps, "delete_run_redis_keys", AsyncMock(return_value=0)) as redis_mock:
        out = _run(ps.PrivacyService(db, tenant_id=TEST_TENANT).erase_user_data(
            TEST_USER, blob_store=FakeBlobStore(), settings=_settings(),
            actor_user_id=TEST_USER,
        ))
    sync.commit()

    assert out["deleted"]["sessions"] == 2
    assert out["deleted"]["messages"] == 1 and out["deleted"]["runs"] == 1
    remaining = set(sync.execute(select(Session.id)).scalars())
    assert remaining == {"ses_S2", "ses_U", "ses_F", "ses_X"}
    cancels = [url for m, url, _ in http.calls if m == "POST"]
    assert cancels == [f"{SCHED}/runs/ctr_s1/cancel"]
    # Memory docs follow the session's real owner (the service account).
    memory = {kw["params"]["origin_session_id"]: kw["headers"]["X-User-Id"]
              for m, _, kw in http.calls if m == "DELETE"}
    assert memory == {"ses_A": TEST_USER, "ses_S1": CRON_USER}
    assert redis_mock.call_args.args[1] == ["run_S1"]


def test_on_behalf_criterion_compiles_for_postgres() -> None:
    stmt = select(Session.id).where(ps._subject_sessions(TEST_TENANT, TEST_USER, _settings()))
    sql = str(stmt.compile(dialect=postgresql.dialect()))
    assert "(session_orchestrator.session.metadata ->> %(metadata_1)s)" in sql
    assert "session_orchestrator.session.user_id IN (" in sql


# ── erasure ──────────────────────────────────────────────────────────────────

_EXPECTED_COUNTS = {
    "sessions": 2,
    "messages": 4,
    "runs": 4,
    "attachments": 3,
    "canvases": 2,
    "canvas_versions": 3,
    "canvas_suggestions": 1,
    "workspaces": 2,
    "workspace_files": 2,
    "workspace_file_versions": 2,
}


def test_erase_dry_run_counts_without_side_effects() -> None:
    db, sync = new_db()
    _seed(sync)
    blobs = FakeBlobStore()
    http = RecordingHttpClient()
    with patch.object(ps.httpx, "AsyncClient", http), \
            patch.object(ps, "delete_run_redis_keys", AsyncMock()) as redis_mock:
        out = _run(ps.PrivacyService(db, tenant_id=TEST_TENANT).erase_user_data(
            TEST_USER, dry_run=True, blob_store=blobs, settings=_settings(),
        ))
    assert out["dry_run"] is True
    assert out["subject_user_id"] == TEST_USER
    assert out["deleted"] == {**_EXPECTED_COUNTS, "blobs": 5}  # candidate keys
    assert out["runs_cancelled"] == 2  # run_A2 + run_B1; inline run_A3 skipped
    assert http.calls == [] and blobs.deleted == []
    redis_mock.assert_not_awaited()
    sync.rollback()
    assert _count(sync, Session, user_id=TEST_USER) == 2


def test_erase_hard_deletes_everything_in_fk_order() -> None:
    db, sync = new_db()
    _seed(sync)
    blobs = FakeBlobStore()
    http = RecordingHttpClient()
    with patch.object(ps.httpx, "AsyncClient", http), \
            patch.object(ps, "delete_run_redis_keys", AsyncMock(return_value=0)) as redis_mock:
        out = _run(ps.PrivacyService(db, tenant_id=TEST_TENANT).erase_user_data(
            TEST_USER, blob_store=blobs, settings=_settings(), actor_user_id=TEST_USER,
        ))
    sync.commit()

    assert out == {
        "subject_user_id": TEST_USER,
        "dry_run": False,
        "deleted": {**_EXPECTED_COUNTS, "blobs": 4},
        "runs_cancelled": 2,
    }
    # k/shared is still referenced by the other user's attachment → kept.
    assert sorted(blobs.deleted) == ["k/in-a1", "k/out-a2", "k/ws-1-v1", "k/ws-1-v3"]

    for model in ALL_MODELS:
        if model is UserPrivacySettings:
            continue
        owner_col = "user_id" if hasattr(model, "user_id") else None
        if owner_col:
            assert _count(sync, model, user_id=TEST_USER) == 0, model.__name__
    # The opt-out survives: it keeps memory off if the user goes on.
    remaining = sync.execute(
        select(UserPrivacySettings.memory_enabled).where(
            UserPrivacySettings.user_id == TEST_USER,
        )
    ).scalars().all()
    assert remaining == [False]
    assert _count(sync, Message, session_id="ses_A") == 0
    assert _count(sync, CanvasVersion) == 0 and _count(sync, CanvasSuggestion) == 0
    assert _count(sync, WorkspaceFileVersion) == 0
    # The other user's data is untouched.
    assert _count(sync, Session, user_id=OTHER_USER) == 1
    assert _count(sync, Message, session_id="ses_O") == 1
    assert _count(sync, Run, session_id="ses_O") == 1
    assert _count(sync, Attachment, user_id=OTHER_USER) == 1
    assert _count(sync, WorkspaceFile, user_id=OTHER_USER) == 1
    assert _count(sync, UserPrivacySettings, user_id=OTHER_USER) == 1

    cancels = [(url, kw["headers"]) for m, url, kw in http.calls if m == "POST"]
    assert sorted(url for url, _ in cancels) == [
        f"{SCHED}/runs/ctr_a2/cancel", f"{SCHED}/runs/ctr_b1/cancel",
    ]
    assert all(h == {"X-Tenant-Id": TEST_TENANT, "X-User-Id": TEST_USER} for _, h in cancels)
    memory = [(url, kw) for m, url, kw in http.calls if m == "DELETE"]
    assert sorted(kw["params"]["origin_session_id"] for _, kw in memory) == ["ses_A", "ses_B"]
    assert all(url == f"{KI}/memories" for url, _ in memory)
    assert all(kw["headers"]["X-User-Id"] == TEST_USER for _, kw in memory)
    assert all("X-Actor" not in kw["headers"] for _, kw in memory)  # self-service

    redis_mock.assert_awaited_once()
    args, kwargs = redis_mock.call_args
    assert args[0] == "redis://redis:6379/0"
    assert sorted(args[1]) == ["run_A1", "run_A2", "run_A3", "run_B1"]
    assert kwargs == {"scan": True}


def test_erase_is_idempotent() -> None:
    db, sync = new_db()
    _seed(sync)
    with patch.object(ps.httpx, "AsyncClient", RecordingHttpClient()), \
            patch.object(ps, "delete_run_redis_keys", AsyncMock(return_value=0)):
        svc = ps.PrivacyService(db, tenant_id=TEST_TENANT)
        _run(svc.erase_user_data(TEST_USER, blob_store=FakeBlobStore(), settings=_settings()))
        again = _run(svc.erase_user_data(
            TEST_USER, blob_store=FakeBlobStore(), settings=_settings(),
        ))
    assert set(again["deleted"].values()) == {0}
    assert again["runs_cancelled"] == 0


def test_erase_drops_a_settings_row_that_only_says_enabled() -> None:
    db, sync = new_db()
    sync.add(UserPrivacySettings(tenant_id=TEST_TENANT, user_id=TEST_USER, memory_enabled=True))
    sync.commit()
    with patch.object(ps.httpx, "AsyncClient", RecordingHttpClient()), \
            patch.object(ps, "delete_run_redis_keys", AsyncMock(return_value=0)):
        _run(ps.PrivacyService(db, tenant_id=TEST_TENANT).erase_user_data(
            TEST_USER, blob_store=FakeBlobStore(), settings=_settings(),
        ))
    sync.commit()
    assert _count(sync, UserPrivacySettings, user_id=TEST_USER) == 0
    assert _run(ps.memory_enabled_for(db, tenant_id=TEST_TENANT, user_id=TEST_USER)) is True


def test_admin_erase_forwards_admin_actor() -> None:
    db, sync = new_db()
    _seed(sync)
    http = RecordingHttpClient()
    admin = "usr_01J5TEST0000000000000ADMIN"
    with patch.object(ps.httpx, "AsyncClient", http), \
            patch.object(ps, "delete_run_redis_keys", AsyncMock(return_value=0)):
        _run(ps.PrivacyService(db, tenant_id=TEST_TENANT).erase_user_data(
            TEST_USER, blob_store=FakeBlobStore(), settings=_settings(),
            actor_user_id=admin, as_admin=True,
        ))
    cancel_headers = [kw["headers"] for m, _, kw in http.calls if m == "POST"]
    assert cancel_headers and all(
        h == {"X-Tenant-Id": TEST_TENANT, "X-User-Id": admin, "X-Actor": "ADMIN"}
        for h in cancel_headers
    )
    memory_headers = [kw["headers"] for m, _, kw in http.calls if m == "DELETE"]
    assert memory_headers and all(
        h["X-User-Id"] == TEST_USER and h["X-Actor"] == "ADMIN" for h in memory_headers
    )


def test_erase_aborts_when_a_blob_cannot_be_deleted() -> None:
    """Strict: a failed S3 delete raises so the transaction rolls back and the
    erasure can be retried — no row may vanish while its object survives."""
    db, sync = new_db()
    _seed(sync)
    with patch.object(ps.httpx, "AsyncClient", RecordingHttpClient()), \
            patch.object(ps, "delete_run_redis_keys", AsyncMock(return_value=0)), \
            pytest.raises(ps.PrivacyBlobStoreError) as exc_info:
        _run(ps.PrivacyService(db, tenant_id=TEST_TENANT).erase_user_data(
            TEST_USER, blob_store=FakeBlobStore(fail_on={"k/in-a1"}), settings=_settings(),
        ))
    assert exc_info.value.status == 503
    sync.rollback()  # what the request transaction does on the raised error
    assert _count(sync, Session, user_id=TEST_USER) == 2
    assert _count(sync, Attachment, user_id=TEST_USER) == 3


def test_erase_without_blob_store_refuses_when_objects_exist() -> None:
    db, sync = new_db()
    _seed(sync)
    with patch.object(ps.httpx, "AsyncClient", RecordingHttpClient()), \
            patch.object(ps, "delete_run_redis_keys", AsyncMock(return_value=0)), \
            patch.object(ps, "registered_blob_store", lambda: None), \
            pytest.raises(ps.PrivacyBlobStoreError):
        _run(ps.PrivacyService(db, tenant_id=TEST_TENANT).erase_user_data(
            TEST_USER, blob_store=None, settings=_settings(),
        ))


def test_erase_survives_scheduler_and_knowledge_outage() -> None:
    import httpx

    db, sync = new_db()
    _seed(sync)
    down = RecordingHttpClient(raise_exc=httpx.ConnectError("down"))
    with patch.object(ps.httpx, "AsyncClient", down), \
            patch.object(ps, "delete_run_redis_keys", AsyncMock(return_value=0)):
        out = _run(ps.PrivacyService(db, tenant_id=TEST_TENANT).erase_user_data(
            TEST_USER, blob_store=FakeBlobStore(), settings=_settings(),
        ))
    assert out["runs_cancelled"] == 0
    assert out["deleted"]["sessions"] == 2


# ── blob hygiene ─────────────────────────────────────────────────────────────


def test_delete_unreferenced_blobs_keeps_referenced_keys() -> None:
    db, sync = new_db()
    _seed(sync)
    blobs = FakeBlobStore()
    deleted = _run(ps.delete_unreferenced_blobs(
        db, tenant_id=TEST_TENANT, keys=["k/shared", "k/out-a2", "k/gone", "", "k/gone"],
        blob_store=blobs,
    ))
    assert deleted == 1
    assert blobs.deleted == ["k/gone"]


def test_delete_unreferenced_blobs_best_effort_never_raises() -> None:
    db, sync = new_db()
    blobs = FakeBlobStore(fail_on={"k/a"})
    assert _run(ps.delete_unreferenced_blobs(
        db, tenant_id=TEST_TENANT, keys=["k/a", "k/b"], blob_store=blobs,
    )) == 1
    with patch.object(ps, "registered_blob_store", lambda: None):
        assert _run(ps.delete_unreferenced_blobs(
            db, tenant_id=TEST_TENANT, keys=["k/a"], blob_store=None,
        )) == 0

    async def _boom(*a: Any, **kw: Any) -> Any:
        raise RuntimeError("db down")

    db.execute = _boom  # type: ignore[method-assign]
    untouched = FakeBlobStore()
    assert _run(ps.delete_unreferenced_blobs(
        db, tenant_id=TEST_TENANT, keys=["k/c"], blob_store=untouched,
    )) == 0
    assert untouched.deleted == []  # reference check failed → delete nothing


def test_delete_unreferenced_blobs_uses_registered_store() -> None:
    db, _sync = new_db()
    blobs = FakeBlobStore()
    with patch.object(ps, "registered_blob_store", lambda: blobs):
        _run(ps.delete_unreferenced_blobs(db, tenant_id=TEST_TENANT, keys=["k/x"]))
    assert blobs.deleted == ["k/x"]


# ── knowledge-ingest memory purge ────────────────────────────────────────────


def test_delete_session_memories_contract() -> None:
    http = RecordingHttpClient()
    with patch.object(ps.httpx, "AsyncClient", http):
        ok = _run(ps.delete_session_memories(
            tenant_id=TEST_TENANT, session_id="ses_1", owner_user_id=TEST_USER,
            knowledge_ingest_url=KI,
        ))
        _run(ps.delete_session_memories(
            tenant_id=TEST_TENANT, session_id="ses_2", owner_user_id=TEST_USER,
            as_admin=True, knowledge_ingest_url=KI,
        ))
    assert ok is True
    (m1, url1, kw1), (_, _, kw2) = http.calls
    assert (m1, url1) == ("DELETE", f"{KI}/memories")
    assert kw1["params"] == {"origin_session_id": "ses_1"}
    assert kw1["headers"] == {"X-Tenant-Id": TEST_TENANT, "X-User-Id": TEST_USER}
    assert kw2["headers"]["X-Actor"] == "ADMIN"


def test_delete_session_memories_is_best_effort() -> None:
    import httpx

    with patch.object(ps.httpx, "AsyncClient", RecordingHttpClient(status=503)):
        assert _run(ps.delete_session_memories(
            tenant_id=TEST_TENANT, session_id="ses_1", owner_user_id=TEST_USER,
            knowledge_ingest_url=KI,
        )) is False
    with patch.object(ps.httpx, "AsyncClient", RecordingHttpClient(
        raise_exc=httpx.ConnectError("down"),
    )):
        assert _run(ps.delete_session_memories(
            tenant_id=TEST_TENANT, session_id="ses_1", owner_user_id=TEST_USER,
            knowledge_ingest_url=KI,
        )) is False
    # No knowledge-ingest configured → nothing to do.
    assert _run(ps.delete_session_memories(
        tenant_id=TEST_TENANT, session_id="ses_1", owner_user_id=TEST_USER,
        knowledge_ingest_url="",
    )) is False


# ── Redis hot-state cleanup ──────────────────────────────────────────────────


class _FakeRedis:
    def __init__(self, keys: set[str]) -> None:
        self.keys = keys
        self.closed = False

    async def delete(self, *names: str) -> int:
        hit = [n for n in names if n in self.keys]
        self.keys.difference_update(hit)
        return len(hit)

    async def scan_iter(self, match: str, count: int) -> AsyncIterator[str]:
        assert match == "agent:run:*"
        for key in sorted(self.keys):
            yield key

    async def aclose(self) -> None:
        self.closed = True


def test_delete_run_redis_keys_known_suffixes_and_scan() -> None:
    fake = _FakeRedis({
        "agent:run:run_1:state", "agent:run:run_1:tokens", "agent:run:run_1:paused",
        "agent:run:run_1:custom", "agent:run:run_2:canvas", "agent:run:run_9:state",
    })
    with patch("redis.asyncio.Redis.from_url", return_value=fake):
        n = _run(ps.delete_run_redis_keys("redis://x", ["run_1", "run_2"], scan=True))
    assert n == 5
    assert fake.keys == {"agent:run:run_9:state"}
    assert fake.closed


def test_delete_run_redis_keys_drops_the_dispatch_payload_without_a_scan() -> None:
    # The stored dispatch carries the run's history, memory and prompt; the
    # trash purge deletes without scanning, so the suffix must be named.
    fake = _FakeRedis({"agent:run:run_1:dispatch", "agent:run:run_1:tokens"})
    with patch("redis.asyncio.Redis.from_url", return_value=fake):
        n = _run(ps.delete_run_redis_keys("redis://x", ["run_1"]))
    assert n == 2
    assert fake.keys == set()


def test_delete_run_redis_keys_noop_and_failure_open() -> None:
    assert _run(ps.delete_run_redis_keys(None, ["run_1"])) == 0
    assert _run(ps.delete_run_redis_keys("redis://x", [])) == 0
    with patch("redis.asyncio.Redis.from_url", side_effect=OSError("down")):
        assert _run(ps.delete_run_redis_keys("redis://x", ["run_1"])) == 0


# ── trash purge ──────────────────────────────────────────────────────────────


def _seed_trash(sync: Any) -> None:
    t = TEST_TENANT
    sync.add_all([
        Session(id="ses_T", tenant_id=t, user_id=TEST_USER, status="ACTIVE", deleted_at=ts(40)),
        Session(id="ses_L", tenant_id=t, user_id=TEST_USER, status="ACTIVE"),
        Workspace(id="wsp_T", tenant_id=t, user_id=TEST_USER, name="gone", skills=[], meta={},
                  deleted_at=ts(40)),
    ])
    sync.flush()
    sync.add_all([
        Message(id="msg_T", tenant_id=t, session_id="ses_T", role="user", content="x",
                sequence=1),
        Message(id="msg_L", tenant_id=t, session_id="ses_L", role="user", content="y",
                sequence=1, canvas_id="cvs_L"),
        Run(id="run_T", tenant_id=t, session_id="ses_T", status="COMPLETED",
            task_description="x"),
        Attachment(id="att_T", tenant_id=t, session_id="ses_T", user_id=TEST_USER,
                   filename="a", blob_key="k/t-att"),
        CanvasDocument(id="cvs_T", tenant_id=t, session_id="ses_T", user_id=TEST_USER,
                       kind="document", title="t", deleted_at=ts(40)),
        CanvasDocument(id="cvs_L", tenant_id=t, session_id="ses_L", user_id=TEST_USER,
                       kind="document", title="l", deleted_at=ts(40)),
        WorkspaceFile(id="wsf_T", tenant_id=t, workspace_id="wsp_T", user_id=TEST_USER,
                      filename="f", blob_key="k/t-file"),
    ])
    sync.flush()
    sync.add_all([
        CanvasVersion(id="cvv_T", tenant_id=t, canvas_id="cvs_T", version=1, blocks=[],
                      created_by="model"),
        CanvasVersion(id="cvv_L", tenant_id=t, canvas_id="cvs_L", version=1, blocks=[],
                      created_by="model"),
        WorkspaceFileVersion(id="wfv_T", tenant_id=t, workspace_file_id="wsf_T", version=1,
                             blob_key="k/t-file-v1", size_bytes=1),
    ])
    sync.commit()


def _fake_get_session(db: Any) -> Any:
    calls: list[dict[str, Any]] = []

    async def _gen(**kw: Any) -> AsyncIterator[Any]:
        calls.append(kw)
        yield db

    return _gen, calls


def test_purge_trash_hard_deletes_expired_items_through_the_erasure_path() -> None:
    db, sync = new_db()
    _seed_trash(sync)
    scans: list[tuple[str, dict[str, Any]]] = []

    async def _privileged(settings: Any, sql: str, params: dict[str, Any]) -> list[str]:
        scans.append((sql, params))
        if "FROM session_orchestrator.session" in sql:
            return [f"{TEST_TENANT}\x1fses_T"]
        if "FROM session_orchestrator.workspace" in sql:
            return [f"{TEST_TENANT}\x1fwsp_T"]
        if "canvas_document" in sql:
            return [f"{TEST_TENANT}\x1fcvs_L", "malformed-row"]
        return []

    gen, sessions_opened = _fake_get_session(db)
    blobs = FakeBlobStore()
    http = RecordingHttpClient()
    with patch.object(ps, "privileged_scalars", _privileged), \
            patch.object(ps, "get_session", gen), \
            patch.object(ps.httpx, "AsyncClient", http), \
            patch.object(ps, "delete_run_redis_keys", AsyncMock(return_value=0)) as redis_mock:
        stats = _run(ps.purge_trash(_settings(), grace_days=30, blob_store=blobs))
    sync.commit()

    assert stats == {"sessions": 1, "workspaces": 1, "canvases": 1, "failed": 0}
    assert len(scans) == 3
    for sql, params in scans:
        assert "deleted_at IS NOT NULL" in sql and "< :cutoff" in sql
        age = dt.datetime.now(tz=dt.UTC) - params["cutoff"]
        assert dt.timedelta(days=29, hours=23) < age < dt.timedelta(days=30, hours=1)
    assert "s.deleted_at IS NULL" in scans[2][0]  # canvases of live sessions only
    assert all(kw == {"tenant_id": TEST_TENANT, "user_id": "SYSTEM", "actor": "SYSTEM"}
               for kw in sessions_opened)

    assert _count(sync, Session, id="ses_T") == 0
    assert _count(sync, Message, session_id="ses_T") == 0
    assert _count(sync, Run, session_id="ses_T") == 0
    assert _count(sync, CanvasDocument, id="cvs_T") == 0
    assert _count(sync, Workspace, id="wsp_T") == 0
    assert _count(sync, WorkspaceFileVersion) == 0
    assert _count(sync, CanvasDocument, id="cvs_L") == 0
    assert _count(sync, CanvasVersion) == 0
    # The live session (and its message) stays.
    assert _count(sync, Session, id="ses_L") == 1
    assert _count(sync, Message, session_id="ses_L") == 1
    assert sorted(blobs.deleted) == ["k/t-att", "k/t-file", "k/t-file-v1"]

    memory_calls = [c for c in http.calls if c[0] == "DELETE"]
    assert len(memory_calls) == 1
    memory_call = memory_calls[0]
    assert memory_call[2]["params"] == {"origin_session_id": "ses_T"}
    assert memory_call[2]["headers"]["X-Actor"] == "ADMIN"
    assert ["run_T"] in [list(c.args[1]) for c in redis_mock.call_args_list]


def test_purge_trash_isolates_a_failing_item() -> None:
    db, sync = new_db()
    _seed_trash(sync)

    async def _privileged(settings: Any, sql: str, params: dict[str, Any]) -> list[str]:
        if "FROM session_orchestrator.session" in sql:
            return [f"{TEST_TENANT}\x1fses_T"]
        if "FROM session_orchestrator.workspace" in sql:
            return [f"{TEST_TENANT}\x1fwsp_T"]
        return []

    gen, _ = _fake_get_session(db)
    with patch.object(ps, "privileged_scalars", _privileged), \
            patch.object(ps, "get_session", gen), \
            patch.object(ps.httpx, "AsyncClient", RecordingHttpClient()), \
            patch.object(ps, "delete_run_redis_keys", AsyncMock(return_value=0)):
        stats = _run(ps.purge_trash(
            _settings(), grace_days=30, blob_store=FakeBlobStore(fail_on={"k/t-att"}),
        ))
    assert stats["failed"] == 1
    assert stats["workspaces"] == 1


def test_purge_trash_disabled_by_negative_grace() -> None:
    scan = AsyncMock()
    with patch.object(ps, "privileged_scalars", scan):
        stats = _run(ps.purge_trash(_settings(), grace_days=-1))
    assert stats == {"sessions": 0, "workspaces": 0, "canvases": 0, "failed": 0}
    scan.assert_not_awaited()
