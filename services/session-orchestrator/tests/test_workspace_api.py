"""Tests for the workspace API + workspace run-context helpers.

Covers, in mock-DB style (same conventions as ``test_attachments.py``):

* workspace CRUD — list, lazy default creation (incl. the unique-index race),
  create, get, patch, soft-delete, default-delete 409, not-found/not-owner 404;
* workspace files — upload (blob key layout + 413 limit), list, delete
  (best-effort blob delete), content download (RFC 6266 non-ASCII filename);
* ``load_workspace_context`` — None without workspace_id / for a deleted
  workspace, full context otherwise;
* the pure helpers ``_merge_skills`` (runs.py) and
  ``build_workspace_files_block`` (attachment_text_extractor).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import io
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from agent_session_orchestrator.domain.models import Workspace, WorkspaceFile
from tests._helpers import TEST_TENANT, TEST_USER, make_scalar_result

_NOW = dt.datetime(2026, 7, 16, 0, 0, 0, tzinfo=dt.UTC)


def _fake_workspace(
    ws_id: str = "wsp_001",
    *,
    is_default: bool = False,
    user_id: str = TEST_USER,
    instructions: str | None = "写作必须使用正式书面语。",
    skills: list[str] | None = None,
    sensitivity_level: str = "L2",
) -> Workspace:
    return Workspace(
        id=ws_id,
        tenant_id=TEST_TENANT,
        user_id=user_id,
        name="工作区",
        description=None,
        instructions=instructions,
        skills=skills if skills is not None else ["writer"],
        is_default=is_default,
        meta={"glyph": "◇", "accent": "#d23a1f"},
        sensitivity_level=sensitivity_level,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _fake_ws_file(
    file_id: str = "wsf_001",
    ws_id: str = "wsp_001",
    *,
    filename: str = "handbook.txt",
    size_bytes: int = 12,
    sensitivity_level: str = "L2",
) -> WorkspaceFile:
    return WorkspaceFile(
        id=file_id,
        tenant_id=TEST_TENANT,
        workspace_id=ws_id,
        user_id=TEST_USER,
        filename=filename,
        mime="text/plain",
        size_bytes=size_bytes,
        blob_key=f"tenant/{TEST_TENANT}/workspaces/{ws_id}/files/{file_id}/{filename}",
        description=None,
        sensitivity_level=sensitivity_level,
        created_at=_NOW,
    )


def _count_result(n: int) -> MagicMock:
    """Fake SQLAlchemy Result for the ``select(func.count())`` query."""
    result = MagicMock()
    result.scalar_one = MagicMock(return_value=n)
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Workspace CRUD
# ─────────────────────────────────────────────────────────────────────────────


def test_list_workspaces_returns_rows_with_file_count(
    app_client: TestClient, mock_db: AsyncMock, auth_headers: dict,
) -> None:
    ws = _fake_workspace()
    mock_db.execute.side_effect = [
        make_scalar_result([ws]),  # list_workspaces
        _count_result(2),          # count_files
    ]
    resp = app_client.get("/api/session/v1/workspaces", headers=auth_headers)
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["id"] == "wsp_001"
    assert items[0]["name"] == "工作区"
    assert items[0]["skills"] == ["writer"]
    assert items[0]["file_count"] == 2
    assert items[0]["meta"]["glyph"] == "◇"


def test_list_workspaces_lazy_creates_default(
    app_client: TestClient, mock_db: AsyncMock, auth_headers: dict, monkeypatch,
) -> None:
    """A user with zero live workspaces gets the default one created (in its
    own short transaction) and returned by the re-query."""
    from agent_session_orchestrator.api.v1 import workspaces as ws_mod

    lazy_db = AsyncMock()
    lazy_db.add = MagicMock()

    async def _fake_acquire(*, tenant_id, user_id="SYSTEM", actor="USER"):
        yield lazy_db

    monkeypatch.setattr(ws_mod, "acquire_db_session", _fake_acquire)

    created = _fake_workspace(is_default=True)
    mock_db.execute.side_effect = [
        make_scalar_result([]),        # first list: empty
        make_scalar_result([created]),  # re-query after lazy create
        _count_result(0),
    ]
    resp = app_client.get("/api/session/v1/workspaces", headers=auth_headers)
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["is_default"] is True

    # The lazy transaction actually created the default row with the fixed
    # name/meta/is_default the spec mandates.
    added = lazy_db.add.call_args[0][0]
    assert isinstance(added, Workspace)
    assert added.is_default is True
    assert added.name == "工作区"
    assert added.meta == {"glyph": "◇", "accent": "#d23a1f"}


def test_list_workspaces_lazy_create_race_is_benign(
    app_client: TestClient, mock_db: AsyncMock, auth_headers: dict, monkeypatch,
) -> None:
    """A concurrent lazy-create losing the unique-index race (IntegrityError)
    is swallowed; the re-query returns the winner's row."""
    from agent_session_orchestrator.api.v1 import workspaces as ws_mod

    lazy_db = AsyncMock()
    lazy_db.add = MagicMock()
    lazy_db.flush = AsyncMock(
        side_effect=IntegrityError("stmt", {}, Exception("duplicate key"))
    )

    async def _fake_acquire(*, tenant_id, user_id="SYSTEM", actor="USER"):
        yield lazy_db

    monkeypatch.setattr(ws_mod, "acquire_db_session", _fake_acquire)

    winner = _fake_workspace(is_default=True)
    mock_db.execute.side_effect = [
        make_scalar_result([]),        # first list: empty
        make_scalar_result([winner]),  # re-query finds the winner's row
        _count_result(0),
    ]
    resp = app_client.get("/api/session/v1/workspaces", headers=auth_headers)
    assert resp.status_code == 200
    assert len(resp.json()["items"]) == 1


def test_create_workspace(
    app_client: TestClient, mock_db: AsyncMock, auth_headers: dict,
) -> None:
    async def _refresh(obj: Workspace) -> None:
        obj.id = "wsp_new"
        obj.created_at = _NOW
        obj.updated_at = _NOW

    mock_db.refresh.side_effect = _refresh
    mock_db.execute.side_effect = [_count_result(0)]

    resp = app_client.post(
        "/api/session/v1/workspaces",
        headers=auth_headers,
        json={
            "name": "研究工作区",
            "description": "论文相关",
            "instructions": "引用要带出处",
            "skills": ["researcher"],
            "meta": {"glyph": "◆"},
            "sensitivity_level": "L1",
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["id"] == "wsp_new"
    assert body["name"] == "研究工作区"
    assert body["instructions"] == "引用要带出处"
    assert body["skills"] == ["researcher"]
    assert body["is_default"] is False
    assert body["file_count"] == 0
    # sensitivity_level round-trips (L1 <= caller L2 clearance, so allowed).
    assert body["sensitivity_level"] == "L1"


def test_create_workspace_requires_name(
    app_client: TestClient, mock_db: AsyncMock, auth_headers: dict,
) -> None:
    resp = app_client.post(
        "/api/session/v1/workspaces", headers=auth_headers, json={},
    )
    assert resp.status_code == 422


def test_get_workspace_detail(
    app_client: TestClient, mock_db: AsyncMock, auth_headers: dict,
) -> None:
    ws = _fake_workspace()
    mock_db.execute.side_effect = [
        make_scalar_result(ws),  # get_workspace
        _count_result(1),        # count_files
    ]
    resp = app_client.get("/api/session/v1/workspaces/wsp_001", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["id"] == "wsp_001"
    assert resp.json()["file_count"] == 1


def test_get_workspace_not_found_or_not_owner_404(
    app_client: TestClient, mock_db: AsyncMock, auth_headers: dict,
) -> None:
    """Not-exists, soft-deleted and not-owned all surface as the same 404 —
    the owner filter is part of the lookup query."""
    mock_db.execute.side_effect = [make_scalar_result(None)]
    resp = app_client.get("/api/session/v1/workspaces/wsp_zzz", headers=auth_headers)
    assert resp.status_code == 404
    assert resp.json()["code"] == "AGENT.WORKSPACE.NOT_FOUND"


def test_patch_workspace(
    app_client: TestClient, mock_db: AsyncMock, auth_headers: dict,
) -> None:
    ws = _fake_workspace()
    mock_db.execute.side_effect = [
        make_scalar_result(ws),  # get_workspace
        _count_result(0),        # count_files
    ]
    resp = app_client.patch(
        "/api/session/v1/workspaces/wsp_001",
        headers=auth_headers,
        json={"name": "改名了", "skills": ["a", "b"]},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["name"] == "改名了"
    assert resp.json()["skills"] == ["a", "b"]
    # Untouched fields keep their values (exclude_unset semantics).
    assert resp.json()["instructions"] == "写作必须使用正式书面语。"


def test_delete_workspace_soft_deletes(
    app_client: TestClient, mock_db: AsyncMock, auth_headers: dict,
) -> None:
    ws = _fake_workspace()
    mock_db.execute.side_effect = [make_scalar_result(ws)]
    resp = app_client.delete(
        "/api/session/v1/workspaces/wsp_001", headers=auth_headers,
    )
    assert resp.status_code == 204
    assert ws.deleted_at is not None


def test_delete_default_workspace_409(
    app_client: TestClient, mock_db: AsyncMock, auth_headers: dict,
) -> None:
    ws = _fake_workspace(is_default=True)
    mock_db.execute.side_effect = [make_scalar_result(ws)]
    resp = app_client.delete(
        "/api/session/v1/workspaces/wsp_001", headers=auth_headers,
    )
    assert resp.status_code == 409
    assert resp.json()["code"] == "AGENT.WORKSPACE.DEFAULT_DELETE"
    assert ws.deleted_at is None


# ─────────────────────────────────────────────────────────────────────────────
# Workspace files
# ─────────────────────────────────────────────────────────────────────────────


def test_upload_workspace_file(
    app_client: TestClient, mock_db: AsyncMock, auth_headers: dict,
) -> None:
    ws = _fake_workspace()

    async def _refresh(obj: WorkspaceFile) -> None:
        obj.id = "wsf_001"
        obj.created_at = _NOW

    mock_db.execute.side_effect = [make_scalar_result(ws)]  # ownership check
    mock_db.refresh.side_effect = _refresh

    fake_blob = MagicMock()
    with patch.object(app_client.app.state, "blob_store", fake_blob, create=True):
        resp = app_client.post(
            "/api/session/v1/workspaces/wsp_001/files",
            headers=auth_headers,
            files={"file": ("handbook.txt", io.BytesIO(b"hello world!"), "text/plain")},
            data={"description": "团队手册", "sensitivity_level": "L1"},
        )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["id"] == "wsf_001"
    assert body["workspace_id"] == "wsp_001"
    assert body["filename"] == "handbook.txt"
    assert body["size_bytes"] == 12
    assert body["description"] == "团队手册"
    # sensitivity_level round-trips (L1 <= workspace L2 + caller L2).
    assert body["sensitivity_level"] == "L1"

    # Blob key layout: tenant/{t}/workspaces/{ws}/files/{wsf}/{filename}.
    key, data = fake_blob.put.call_args[0][0], fake_blob.put.call_args[0][1]
    assert key == f"tenant/{TEST_TENANT}/workspaces/wsp_001/files/wsf_001/handbook.txt"
    assert data == b"hello world!"


def test_upload_workspace_file_over_limit_413(
    app_client: TestClient, mock_db: AsyncMock, auth_headers: dict,
) -> None:
    fake_blob = MagicMock()
    with patch.object(app_client.app.state, "blob_store", fake_blob, create=True), \
            patch.object(app_client.app.state.settings, "workspace_file_max_bytes", 10):
        resp = app_client.post(
            "/api/session/v1/workspaces/wsp_001/files",
            headers=auth_headers,
            files={"file": ("big.bin", io.BytesIO(b"x" * 11), "application/octet-stream")},
        )
    assert resp.status_code == 413
    fake_blob.put.assert_not_called()


def test_list_workspace_files(
    app_client: TestClient, mock_db: AsyncMock, auth_headers: dict,
) -> None:
    ws = _fake_workspace()
    wsf = _fake_ws_file()
    mock_db.execute.side_effect = [
        make_scalar_result(ws),    # ownership check
        make_scalar_result([wsf]),  # list_files
    ]
    resp = app_client.get(
        "/api/session/v1/workspaces/wsp_001/files", headers=auth_headers,
    )
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["id"] == "wsf_001"
    assert items[0]["filename"] == "handbook.txt"


def test_delete_workspace_file_row_and_blob(
    app_client: TestClient, mock_db: AsyncMock, auth_headers: dict,
) -> None:
    ws = _fake_workspace()
    wsf = _fake_ws_file()
    mock_db.execute.side_effect = [
        make_scalar_result(ws),   # ownership check
        make_scalar_result(wsf),  # get_file
    ]
    fake_blob = MagicMock()
    with patch.object(app_client.app.state, "blob_store", fake_blob, create=True):
        resp = app_client.delete(
            "/api/session/v1/workspaces/wsp_001/files/wsf_001", headers=auth_headers,
        )
    assert resp.status_code == 204
    mock_db.delete.assert_called_once_with(wsf)
    fake_blob.delete.assert_called_once_with(wsf.blob_key)


def test_delete_workspace_file_blob_failure_still_204(
    app_client: TestClient, mock_db: AsyncMock, auth_headers: dict,
) -> None:
    ws = _fake_workspace()
    wsf = _fake_ws_file()
    mock_db.execute.side_effect = [
        make_scalar_result(ws),
        make_scalar_result(wsf),
    ]
    fake_blob = MagicMock()
    fake_blob.delete.side_effect = RuntimeError("s3 down")
    with patch.object(app_client.app.state, "blob_store", fake_blob, create=True):
        resp = app_client.delete(
            "/api/session/v1/workspaces/wsp_001/files/wsf_001", headers=auth_headers,
        )
    assert resp.status_code == 204  # blob delete is best-effort


def test_download_workspace_file_content_rfc6266(
    app_client: TestClient, mock_db: AsyncMock, auth_headers: dict,
) -> None:
    """Non-ASCII filename is served via the RFC 6266 ``filename*`` form."""
    ws = _fake_workspace()
    wsf = _fake_ws_file(filename="报告.txt")
    mock_db.execute.side_effect = [
        make_scalar_result(ws),   # ownership check
        make_scalar_result(wsf),  # get_file
    ]
    fake_blob = MagicMock()
    fake_blob.get.return_value = "工作区文件内容".encode()
    with patch.object(app_client.app.state, "blob_store", fake_blob, create=True):
        resp = app_client.get(
            "/api/session/v1/workspaces/wsp_001/files/wsf_001/content",
            headers=auth_headers,
        )
    assert resp.status_code == 200
    assert resp.content == "工作区文件内容".encode()
    disposition = resp.headers["Content-Disposition"]
    assert "filename*=UTF-8''%E6%8A%A5%E5%91%8A.txt" in disposition


def test_workspace_file_not_found_404(
    app_client: TestClient, mock_db: AsyncMock, auth_headers: dict,
) -> None:
    ws = _fake_workspace()
    mock_db.execute.side_effect = [
        make_scalar_result(ws),    # ownership check passes
        make_scalar_result(None),  # file lookup misses
    ]
    resp = app_client.get(
        "/api/session/v1/workspaces/wsp_001/files/wsf_zzz/content",
        headers=auth_headers,
    )
    assert resp.status_code == 404
    assert resp.json()["code"] == "AGENT.WORKSPACE.FILE_NOT_FOUND"


# ─────────────────────────────────────────────────────────────────────────────
# load_workspace_context
# ─────────────────────────────────────────────────────────────────────────────


def _run_coro(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_load_workspace_context_none_without_workspace_id() -> None:
    from agent_session_orchestrator.services.workspace_context import (
        load_workspace_context,
    )

    db = AsyncMock()
    session = MagicMock()
    session.workspace_id = None
    assert _run_coro(load_workspace_context(db, session, user_clearance="L2")) is None
    db.execute.assert_not_called()


def test_load_workspace_context_deleted_workspace_returns_none() -> None:
    from agent_session_orchestrator.services.workspace_context import (
        load_workspace_context,
    )

    db = AsyncMock()
    db.execute.side_effect = [make_scalar_result(None)]  # soft-deleted / gone
    session = MagicMock()
    session.workspace_id = "wsp_gone"
    session.tenant_id = TEST_TENANT
    assert _run_coro(load_workspace_context(db, session, user_clearance="L2")) is None


def test_load_workspace_context_returns_full_context() -> None:
    from agent_session_orchestrator.services.workspace_context import (
        load_workspace_context,
    )

    ws = _fake_workspace(skills=["writer", "researcher"])
    wsf = _fake_ws_file()
    db = AsyncMock()
    db.execute.side_effect = [
        make_scalar_result(ws),     # workspace lookup
        make_scalar_result([wsf]),  # files
    ]
    session = MagicMock()
    session.workspace_id = "wsp_001"
    session.tenant_id = TEST_TENANT

    ctx = _run_coro(load_workspace_context(db, session, user_clearance="L2"))
    assert ctx is not None
    assert ctx.instructions == "写作必须使用正式书面语。"
    assert ctx.skills == ["writer", "researcher"]
    assert len(ctx.files) == 1 and ctx.files[0].filename == "handbook.txt"


def test_load_workspace_context_blank_instructions_become_none() -> None:
    from agent_session_orchestrator.services.workspace_context import (
        load_workspace_context,
    )

    ws = _fake_workspace(instructions="   ", skills=[])
    db = AsyncMock()
    db.execute.side_effect = [
        make_scalar_result(ws),
        make_scalar_result([]),
    ]
    session = MagicMock()
    session.workspace_id = "wsp_001"
    session.tenant_id = TEST_TENANT

    ctx = _run_coro(load_workspace_context(db, session, user_clearance="L2"))
    assert ctx is not None
    assert ctx.instructions is None
    assert ctx.skills == []


# ─────────────────────────────────────────────────────────────────────────────
# Pure helpers: skills merge + workspace files block
# ─────────────────────────────────────────────────────────────────────────────


def test_merge_skills_union_order_dedupe() -> None:
    from agent_session_orchestrator.api.v1.runs import _merge_skills

    assert _merge_skills(["a", "b"], ["b", "c", "a", "d"]) == ["a", "b", "c", "d"]
    assert _merge_skills([], ["x", "x"]) == ["x"]
    assert _merge_skills(["k"], []) == ["k"]


def test_prioritize_workspace_files_hoists_referenced_order_preserving() -> None:
    from agent_session_orchestrator.api.v1.runs import _prioritize_workspace_files

    files = [
        {"id": "wsf_1", "filename": "a"},
        {"id": "wsf_2", "filename": "b"},
        {"id": "wsf_3", "filename": "c"},
        {"id": "wsf_4", "filename": "d"},
    ]

    # Referenced files hoisted to the front, each group keeps its ORIGINAL
    # relative order (referenced ordering follows the file list, not the ref list).
    out = _prioritize_workspace_files(files, ["wsf_3", "wsf_1"])
    assert [f["id"] for f in out] == ["wsf_1", "wsf_3", "wsf_2", "wsf_4"]

    # An unknown id (not in the file set) is silently ignored.
    out = _prioritize_workspace_files(files, ["wsf_2", "wsf_999"])
    assert [f["id"] for f in out] == ["wsf_2", "wsf_1", "wsf_3", "wsf_4"]

    # No references → order unchanged (and a fresh list, not the same object).
    out = _prioritize_workspace_files(files, [])
    assert [f["id"] for f in out] == ["wsf_1", "wsf_2", "wsf_3", "wsf_4"]
    assert out is not files


def test_build_workspace_files_block_tags_and_header() -> None:
    from agent_session_orchestrator.services.attachment_text_extractor import (
        build_workspace_files_block,
    )

    block = build_workspace_files_block([("wsf_1", "规范.md", "所有接口必须带鉴权。")])
    assert block is not None
    assert "工作区" in block
    # The wsf id is surfaced as an id attribute for the file-link convention.
    assert '<workspace_file id="wsf_1" filename="规范.md">' in block
    assert "所有接口必须带鉴权。" in block
    assert build_workspace_files_block([]) is None


def test_build_workspace_files_block_caps_files_and_lists_omitted() -> None:
    from agent_session_orchestrator.services.attachment_text_extractor import (
        build_workspace_files_block,
    )

    files = [(f"wsf_{i}", f"f{i}.txt", f"内容{i}") for i in range(12)]
    block = build_workspace_files_block(files, max_files=10)
    assert block is not None
    assert '<workspace_file id="wsf_9" filename="f9.txt">' in block
    assert '<workspace_file id="wsf_10" filename="f10.txt">' not in block
    # Omitted files are listed by name.
    assert "f10.txt" in block and "f11.txt" in block
    assert "未注入" in block


def test_build_workspace_files_block_enforces_budget() -> None:
    from agent_session_orchestrator.services.attachment_text_extractor import (
        build_workspace_files_block,
    )

    block = build_workspace_files_block(
        [("wsf_a", "a.txt", "x" * 100), ("wsf_b", "b.txt", "y" * 100)],
        session_budget=100,
    )
    assert block is not None
    assert '<workspace_file id="wsf_a" filename="a.txt">' in block
    # b.txt blew the budget: not injected, listed as omitted.
    assert '<workspace_file filename="b.txt">' not in block
    assert "b.txt" in block


# ─────────────────────────────────────────────────────────────────────────────
# Sensitivity level / clearance
# ─────────────────────────────────────────────────────────────────────────────


def test_create_workspace_level_above_caller_clearance_403(
    app_client: TestClient, mock_db: AsyncMock, auth_headers: dict,
) -> None:
    """auth_headers bakes X-User-Clearance: L2; creating an L4 workspace is 403."""
    resp = app_client.post(
        "/api/session/v1/workspaces",
        headers=auth_headers,
        json={"name": "机密区", "sensitivity_level": "L4"},
    )
    assert resp.status_code == 403
    assert resp.json()["code"] == "AGENT.WORKSPACE.CLEARANCE_DENIED"


def test_upload_file_level_above_workspace_422(
    app_client: TestClient, mock_db: AsyncMock, auth_headers: dict,
) -> None:
    """File L4 into an L2 workspace is 422 - but only after the caller-clearance
    gate, so the caller is cleared for L4 (header override)."""
    ws = _fake_workspace()  # L2 workspace
    mock_db.execute.side_effect = [make_scalar_result(ws)]  # ownership check
    headers = {**auth_headers, "X-User-Clearance": "L4"}
    fake_blob = MagicMock()
    with patch.object(app_client.app.state, "blob_store", fake_blob, create=True):
        resp = app_client.post(
            "/api/session/v1/workspaces/wsp_001/files",
            headers=headers,
            files={"file": ("secret.txt", io.BytesIO(b"top secret"), "text/plain")},
            data={"sensitivity_level": "L4"},
        )
    assert resp.status_code == 422
    assert resp.json()["code"] == "AGENT.WORKSPACE.FILE_LEVEL_EXCEEDS_WORKSPACE"
    fake_blob.put.assert_not_called()


def test_upload_file_level_above_caller_clearance_403(
    app_client: TestClient, mock_db: AsyncMock, auth_headers: dict,
) -> None:
    """File L4 into an L4 workspace with an L2 caller is 403 (clearance gate
    fires before the file<=workspace check)."""
    ws = _fake_workspace(sensitivity_level="L4")  # L4 workspace
    mock_db.execute.side_effect = [make_scalar_result(ws)]
    # auth_headers bakes L2 clearance.
    fake_blob = MagicMock()
    with patch.object(app_client.app.state, "blob_store", fake_blob, create=True):
        resp = app_client.post(
            "/api/session/v1/workspaces/wsp_001/files",
            headers=auth_headers,
            files={"file": ("secret.txt", io.BytesIO(b"top secret"), "text/plain")},
            data={"sensitivity_level": "L4"},
        )
    assert resp.status_code == 403
    assert resp.json()["code"] == "AGENT.WORKSPACE.CLEARANCE_DENIED"
    fake_blob.put.assert_not_called()


def test_download_file_level_above_caller_clearance_403(
    app_client: TestClient, mock_db: AsyncMock, auth_headers: dict,
) -> None:
    """Downloading an L4 file with L2 clearance is 403 before any bytes are
    served (defence-in-depth alongside DLP)."""
    ws = _fake_workspace(sensitivity_level="L4")
    wsf = _fake_ws_file(sensitivity_level="L4")
    mock_db.execute.side_effect = [
        make_scalar_result(ws),   # ownership check
        make_scalar_result(wsf),  # get_file
    ]
    fake_blob = MagicMock()
    with patch.object(app_client.app.state, "blob_store", fake_blob, create=True):
        resp = app_client.get(
            "/api/session/v1/workspaces/wsp_001/files/wsf_001/content",
            headers=auth_headers,  # L2 clearance
        )
    assert resp.status_code == 403
    assert resp.json()["code"] == "AGENT.WORKSPACE.CLEARANCE_DENIED"
    fake_blob.get.assert_not_called()


def test_load_workspace_context_clearance_below_workspace_returns_none() -> None:
    """Caller L2 against an L4 workspace -> whole context skipped (None)."""
    from agent_session_orchestrator.services.workspace_context import (
        load_workspace_context,
    )

    ws = _fake_workspace(sensitivity_level="L4")
    db = AsyncMock()
    db.execute.side_effect = [make_scalar_result(ws)]  # workspace lookup only
    session = MagicMock()
    session.workspace_id = "wsp_001"
    session.tenant_id = TEST_TENANT

    ctx = _run_coro(load_workspace_context(db, session, user_clearance="L2"))
    assert ctx is None
    # Files query never ran (short-circuited at the workspace-level gate).
    assert db.execute.call_count == 1


def test_load_workspace_context_filters_files_above_caller_clearance() -> None:
    """Caller L3 against an L3 workspace keeps the L2 file, drops the L4 file."""
    from agent_session_orchestrator.services.workspace_context import (
        load_workspace_context,
    )

    ws = _fake_workspace(sensitivity_level="L3")
    wsf_low = _fake_ws_file(file_id="wsf_low", sensitivity_level="L2")
    wsf_high = _fake_ws_file(file_id="wsf_high", sensitivity_level="L4")
    db = AsyncMock()
    db.execute.side_effect = [
        make_scalar_result(ws),                    # workspace lookup
        make_scalar_result([wsf_low, wsf_high]),   # files
    ]
    session = MagicMock()
    session.workspace_id = "wsp_001"
    session.tenant_id = TEST_TENANT

    ctx = _run_coro(load_workspace_context(db, session, user_clearance="L3"))
    assert ctx is not None
    assert [f.id for f in ctx.files] == ["wsf_low"]
