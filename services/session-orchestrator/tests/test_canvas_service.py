"""Tests for CanvasService — CRUD, optimistic lock, accept transaction, revert,
retention cascade — driven against an in-memory fake AsyncSession.

The fake models just enough of the ORM/session contract that CanvasService and
SessionService use (add / flush / refresh / execute over the handful of SELECT
and UPDATE statements they issue) so the real service logic runs without a
Postgres.  RLS (tenant isolation) is enforced by the DB in production and is
exercised by the live_db-style API tests; here the fake honours the explicit
tenant_id predicates the service adds.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from typing import Any

import pytest

from agent_session_orchestrator.domain.models import (
    CanvasDocument,
    CanvasSuggestion,
    CanvasVersion,
    Message,
    Session,
)
from agent_session_orchestrator.errors import (
    CanvasBadRequestError,
    CanvasNotFoundError,
    CanvasSuggestionResolvedError,
    CanvasVersionConflictError,
    CanvasVersionNotFoundError,
    SessionNotFoundError,
)
from agent_session_orchestrator.services.canvas_service import CanvasService
from agent_session_orchestrator.services.session_service import SessionService
from tests._helpers import TEST_TENANT, TEST_USER

_NOW = dt.datetime(2026, 7, 10, tzinfo=dt.UTC)


def _run(coro: Any) -> Any:
    return asyncio.get_event_loop().run_until_complete(coro)


class FakeResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalar_one_or_none(self) -> Any:
        return self._rows[0] if self._rows else None

    def scalars(self) -> FakeResult:
        return self

    def all(self) -> list[Any]:
        return list(self._rows)


class FakeDB:
    """Minimal stateful AsyncSession stand-in backed by python lists."""

    def __init__(self) -> None:
        self.sessions: list[Session] = []
        self.messages: list[Message] = []
        self.canvases: list[CanvasDocument] = []
        self.versions: list[CanvasVersion] = []
        self.suggestions: list[CanvasSuggestion] = []
        self._id_counter = 0

    # -- write side --
    def add(self, obj: Any) -> None:
        if isinstance(obj, CanvasDocument):
            self.canvases.append(obj)
        elif isinstance(obj, CanvasVersion):
            self.versions.append(obj)
        elif isinstance(obj, CanvasSuggestion):
            self.suggestions.append(obj)
        elif isinstance(obj, Message):
            self.messages.append(obj)
        elif isinstance(obj, Session):
            self.sessions.append(obj)

    async def flush(self) -> None:
        # Assign ids to any freshly-added rows lacking one (mimics PK default).
        for bucket, prefix in (
            (self.canvases, "cvs"),
            (self.versions, "cvv"),
            (self.suggestions, "csg"),
        ):
            for row in bucket:
                if getattr(row, "id", None) is None:
                    self._id_counter += 1
                    row.id = f"{prefix}_{self._id_counter:026d}"
                for col in ("created_at", "updated_at"):
                    if hasattr(row, col) and getattr(row, col) is None:
                        setattr(row, col, _NOW)

    async def refresh(self, obj: Any) -> None:
        return None

    async def execute(self, stmt: Any) -> Any:
        # We route by the statement's target entity + a hand-rolled predicate
        # evaluator good enough for the service's queries.  Detect UPDATE by the
        # statement type (an ``is_update`` visit_name), NOT a substring on the
        # compiled SQL — a SELECT of ``updated_at`` also contains "update".
        if getattr(stmt, "is_update", False):
            # Retention cascade soft-delete: flip deleted_at on matching rows.
            params = stmt.compile().params
            session_id = params.get("session_id_1") or params.get("session_id")
            tenant = params.get("tenant_id_1") or params.get("tenant_id")
            now = params.get("deleted_at")
            for c in self.canvases:
                if (
                    c.session_id == session_id
                    and c.tenant_id == tenant
                    and c.deleted_at is None
                ):
                    c.deleted_at = now or _NOW
            return FakeResult([])

        entity = stmt.column_descriptions[0]["entity"]
        params = stmt.compile().params

        def _p(*names: str) -> Any:
            for n in names:
                if n in params:
                    return params[n]
            return None

        # Honour the tenant predicate the service adds (mimics RLS scoping):
        # rows of another tenant are invisible.
        tenant = _p("tenant_id_1", "tenant_id")

        if entity is Session:
            sid = _p("id_1", "id")
            rows = [
                s
                for s in self.sessions
                if s.id == sid and s.tenant_id == tenant and s.deleted_at is None
            ]
            return FakeResult(rows)
        if entity is Message:
            mid = _p("id_1", "id")
            rows = [
                m for m in self.messages if m.id == mid and m.tenant_id == tenant
            ]
            return FakeResult(rows)
        if entity is CanvasDocument:
            cid = _p("id_1")
            sid = _p("session_id_1")
            rows = [
                c
                for c in self.canvases
                if c.tenant_id == tenant and c.deleted_at is None
            ]
            if cid is not None:
                rows = [c for c in rows if c.id == cid]
            if sid is not None:
                rows = [c for c in rows if c.session_id == sid]
            return FakeResult(rows)
        if entity is CanvasVersion:
            cid = _p("canvas_id_1")
            ver = _p("version_1")
            rows = [v for v in self.versions if v.tenant_id == tenant]
            if cid is not None:
                rows = [v for v in rows if v.canvas_id == cid]
            if ver is not None:
                rows = [v for v in rows if v.version == ver]
            rows.sort(key=lambda v: v.version)
            return FakeResult(rows)
        if entity is CanvasSuggestion:
            sid = _p("id_1")
            cid = _p("canvas_id_1")
            rows = [s for s in self.suggestions if s.tenant_id == tenant]
            if sid is not None:
                rows = [s for s in rows if s.id == sid]
            if cid is not None:
                rows = [s for s in rows if s.canvas_id == cid]
            return FakeResult(rows)
        return FakeResult([])


def _seed_session(db: FakeDB, session_id: str = "ses_1") -> Session:
    s = Session(
        id=session_id,
        tenant_id=TEST_TENANT,
        user_id=TEST_USER,
        status="ACTIVE",
        created_at=_NOW,
        updated_at=_NOW,
    )
    db.sessions.append(s)
    return s


def _svc(db: FakeDB) -> CanvasService:
    return CanvasService(db, tenant_id=TEST_TENANT)


# ── create ───────────────────────────────────────────────────────────────────


def test_create_from_content() -> None:
    db = FakeDB()
    _seed_session(db)
    svc = _svc(db)
    canvas = _run(
        svc.create_canvas(
            "ses_1", user_id=TEST_USER, content="# Title\n\nBody text.", kind="report"
        )
    )
    assert canvas.kind == "report"
    assert canvas.current_version == 1
    assert canvas.title == "Title"
    assert len(db.versions) == 1
    assert db.versions[0].version == 1
    assert db.versions[0].created_by == "user"
    assert [b["text"] for b in db.versions[0].blocks] == ["# Title", "Body text."]


def test_create_from_source_message_backfills_canvas_id() -> None:
    db = FakeDB()
    _seed_session(db)
    msg = Message(
        id="msg_1",
        tenant_id=TEST_TENANT,
        session_id="ses_1",
        role="assistant",
        content="好的，我来写。\n\n# 标题\n\n正文。",
        sequence=1,
        created_at=_NOW,
    )
    db.messages.append(msg)
    svc = _svc(db)
    canvas = _run(
        svc.create_canvas("ses_1", user_id=TEST_USER, source_message_id="msg_1")
    )
    assert canvas.source_message_id == "msg_1"
    assert msg.canvas_id == canvas.id  # backfilled
    assert db.versions[0].created_by == "model"
    # Leading ack line stripped by materialize.
    assert "好的，我来写。" not in [b["text"] for b in db.versions[0].blocks]


def test_create_requires_exactly_one_source() -> None:
    db = FakeDB()
    _seed_session(db)
    svc = _svc(db)
    with pytest.raises(CanvasBadRequestError):
        _run(svc.create_canvas("ses_1", user_id=TEST_USER))
    with pytest.raises(CanvasBadRequestError):
        _run(
            svc.create_canvas(
                "ses_1", user_id=TEST_USER, content="x", source_message_id="msg_1"
            )
        )


def test_create_empty_content_rejected() -> None:
    db = FakeDB()
    _seed_session(db)
    svc = _svc(db)
    with pytest.raises(CanvasBadRequestError):
        _run(svc.create_canvas("ses_1", user_id=TEST_USER, content="   "))


def test_create_on_missing_session_rejected() -> None:
    db = FakeDB()  # no session seeded
    svc = _svc(db)
    with pytest.raises(SessionNotFoundError):
        _run(svc.create_canvas("ses_gone", user_id=TEST_USER, content="hi"))


def test_create_on_soft_deleted_session_rejected() -> None:
    db = FakeDB()
    s = _seed_session(db)
    s.deleted_at = _NOW  # soft-deleted
    svc = _svc(db)
    with pytest.raises(SessionNotFoundError):
        _run(svc.create_canvas("ses_1", user_id=TEST_USER, content="hi"))


# ── manual version + optimistic lock ─────────────────────────────────────────


def _make_canvas(db: FakeDB) -> CanvasDocument:
    _seed_session(db)
    svc = _svc(db)
    return _run(svc.create_canvas("ses_1", user_id=TEST_USER, content="v1 body"))


def test_manual_version_advances_and_stamps_user() -> None:
    db = FakeDB()
    canvas = _make_canvas(db)
    svc = _svc(db)
    updated = _run(
        svc.create_manual_version(
            canvas.id,
            blocks=[{"id": "blk_x", "type": "paragraph", "text": "edited"}],
            base_version=1,
        )
    )
    assert updated.current_version == 2
    v2 = [v for v in db.versions if v.version == 2][0]
    assert v2.created_by == "user"
    assert v2.change_summary == "手动编辑"
    assert v2.base_version == 1


def test_manual_version_stale_base_conflicts() -> None:
    db = FakeDB()
    canvas = _make_canvas(db)
    svc = _svc(db)
    with pytest.raises(CanvasVersionConflictError):
        _run(
            svc.create_manual_version(
                canvas.id, blocks=[], base_version=99
            )
        )


def test_get_canvas_missing_raises() -> None:
    db = FakeDB()
    svc = _svc(db)
    with pytest.raises(CanvasNotFoundError):
        _run(svc.get_canvas("cvs_nope"))


# ── suggestions + accept transaction ─────────────────────────────────────────


def _proposal(
    db: FakeDB, canvas: CanvasDocument, *, base_version: int, diffs: list[dict]
) -> CanvasSuggestion:
    svc = _svc(db)
    return _run(
        svc.create_suggestion(
            canvas.id,
            base_version=base_version,
            instruction="make it better",
            diffs=diffs,
            change_summary="改写第一段",
        )
    )


def test_accept_applies_diffs_and_bumps_version() -> None:
    db = FakeDB()
    _seed_session(db)
    svc = _svc(db)
    canvas = _run(
        svc.create_canvas("ses_1", user_id=TEST_USER, content="first\n\nsecond")
    )
    v1_blocks = db.versions[0].blocks
    target_id = v1_blocks[0]["id"]
    sug = _proposal(
        db,
        canvas,
        base_version=1,
        diffs=[{"block_id": target_id, "op": "replace", "sugg": "FIRST!"}],
    )
    updated = _run(svc.accept_suggestion(canvas.id, sug.id))
    assert updated.current_version == 2
    assert sug.status == "accepted"
    assert sug.resolved_at is not None
    v2 = [v for v in db.versions if v.version == 2][0]
    assert v2.created_by == "model"
    assert v2.blocks[0]["text"] == "FIRST!"
    assert v2.blocks[0]["id"] == target_id  # id stability across accept
    # Second block untouched, id preserved.
    assert v2.blocks[1]["id"] == v1_blocks[1]["id"]


def test_accept_stale_base_conflicts() -> None:
    db = FakeDB()
    canvas = _make_canvas(db)
    sug = _proposal(db, canvas, base_version=5, diffs=[])  # base != current(1)
    svc = _svc(db)
    with pytest.raises(CanvasVersionConflictError):
        _run(svc.accept_suggestion(canvas.id, sug.id))


def test_accept_already_resolved_conflicts() -> None:
    db = FakeDB()
    canvas = _make_canvas(db)
    sug = _proposal(db, canvas, base_version=1, diffs=[])
    svc = _svc(db)
    _run(svc.accept_suggestion(canvas.id, sug.id))
    # A second accept must be rejected as already-resolved.
    with pytest.raises(CanvasSuggestionResolvedError):
        _run(svc.accept_suggestion(canvas.id, sug.id))


def test_reject_marks_rejected() -> None:
    db = FakeDB()
    canvas = _make_canvas(db)
    sug = _proposal(db, canvas, base_version=1, diffs=[])
    svc = _svc(db)
    out = _run(svc.reject_suggestion(canvas.id, sug.id))
    assert out.status == "rejected"
    assert out.resolved_at is not None
    # current_version unchanged by a reject.
    assert canvas.current_version == 1


def test_reject_already_resolved_conflicts() -> None:
    db = FakeDB()
    canvas = _make_canvas(db)
    sug = _proposal(db, canvas, base_version=1, diffs=[])
    svc = _svc(db)
    _run(svc.reject_suggestion(canvas.id, sug.id))
    with pytest.raises(CanvasSuggestionResolvedError):
        _run(svc.reject_suggestion(canvas.id, sug.id))


# ── revert ───────────────────────────────────────────────────────────────────


def test_revert_copies_old_blocks_into_new_version() -> None:
    db = FakeDB()
    _seed_session(db)
    svc = _svc(db)
    canvas = _run(svc.create_canvas("ses_1", user_id=TEST_USER, content="original"))
    v1_blocks = list(db.versions[0].blocks)
    # Advance to v2 with different content.
    _run(
        svc.create_manual_version(
            canvas.id,
            blocks=[{"id": "blk_z", "type": "paragraph", "text": "changed"}],
            base_version=1,
        )
    )
    assert canvas.current_version == 2
    # Revert to v1 → v3 with v1's blocks.
    reverted = _run(svc.revert(canvas.id, to_version=1))
    assert reverted.current_version == 3
    v3 = [v for v in db.versions if v.version == 3][0]
    assert v3.blocks == v1_blocks
    assert v3.base_version == 1
    assert "回退" in (v3.change_summary or "")


def test_revert_unknown_version_raises() -> None:
    db = FakeDB()
    canvas = _make_canvas(db)
    svc = _svc(db)
    with pytest.raises(CanvasVersionNotFoundError):
        _run(svc.revert(canvas.id, to_version=99))


# ── retention cascade (design §3.5) ──────────────────────────────────────────


def test_delete_session_soft_deletes_its_canvases() -> None:
    db = FakeDB()
    _seed_session(db)
    csvc = _svc(db)
    canvas = _run(csvc.create_canvas("ses_1", user_id=TEST_USER, content="body"))
    assert canvas.deleted_at is None
    # Soft-delete the session via SessionService (retention path).
    ssvc = SessionService(db, tenant_id=TEST_TENANT)
    _run(ssvc.delete_session("ses_1"))
    assert canvas.deleted_at is not None
    # And the canvas is now invisible to the canvas service.
    with pytest.raises(CanvasNotFoundError):
        _run(csvc.get_canvas(canvas.id))
