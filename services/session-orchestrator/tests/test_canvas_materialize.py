"""Tests for orchestrator-side canvas materialization (canvas v2, Phase B).

Exercises the shared ``_materialize_pending_canvases`` helper (used by both the
streaming loop and the run-completion reconcile path) and the server-authoritative
over-trigger guard ``_canvas_payload_passes_guard``.

Strategy: the Redis read is monkeypatched to return canned payloads, and the real
``CanvasService`` runs against the ``FakeDB`` from ``test_canvas_service`` (a
stateful in-memory AsyncSession stand-in) so the full materialize → version →
message-backfill logic is exercised without Postgres or Redis.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
from typing import Any

import pytest

from agent_session_orchestrator.api.v1 import messages as msg_mod
from agent_session_orchestrator.domain.models import Message, Run
from agent_session_orchestrator.services.session_service import SessionService
from tests._helpers import TEST_TENANT, TEST_USER
from tests.test_canvas_service import FakeDB, _seed_session

_NOW = dt.datetime(2026, 7, 11, tzinfo=dt.UTC)

# A payload comfortably above the guard thresholds (>=200 chars, >=2 blocks).
_LONG = (
    "# 人工智能与未来工作\n\n"
    + ("人工智能正在以前所未有的速度重塑各行各业的工作方式。" * 6)
    + "\n\n"
    + ("从自动化到增强智能，劳动者需要不断学习新的技能以适应变化。" * 6)
)


def _run(coro: Any) -> Any:
    return asyncio.get_event_loop().run_until_complete(coro)


def _make_run(db: FakeDB, run_id: str = "run_cvs", session_id: str = "ses_1") -> Run:
    run = Run(
        id=run_id,
        tenant_id=TEST_TENANT,
        session_id=session_id,
        status="COMPLETED",
        task_description="write me an article",
    )
    return run


def _svc(db: FakeDB) -> SessionService:
    return SessionService(db, tenant_id=TEST_TENANT)


def _patch_redis(monkeypatch: pytest.MonkeyPatch, payloads: list[dict]) -> None:
    async def _fake_read(*, redis_url: str, run_id: str) -> list[dict]:
        return list(payloads)

    monkeypatch.setattr(msg_mod, "_read_canvas_payloads_from_redis", _fake_read)


def _materialize(db: FakeDB, run: Run) -> list[str]:
    return _run(
        msg_mod._materialize_pending_canvases(
            run=run,
            user_id=TEST_USER,
            svc=_svc(db),
            tenant_id=TEST_TENANT,
            redis_url="redis://fake",
        )
    )


# ── guard ─────────────────────────────────────────────────────────────────────


def test_guard_rejects_short_content() -> None:
    assert msg_mod._canvas_payload_passes_guard(
        {"content_markdown": "太短了。"}
    ) is False


def test_guard_accepts_a_single_substantial_block() -> None:
    # The model calls create_canvas deliberately, so one paragraph is enough.
    one_block = "这是一段足够长的内容，" * 30  # no blank-line split
    assert msg_mod._canvas_payload_passes_guard({"content_markdown": one_block}) is True


def test_guard_rejects_a_fragment() -> None:
    fragment = "x" * (msg_mod._CANVAS_MIN_CHARS - 1)
    assert msg_mod._canvas_payload_passes_guard({"content_markdown": f"  {fragment}  "}) is False


def test_guard_accepts_long_multiblock() -> None:
    assert msg_mod._canvas_payload_passes_guard({"content_markdown": _LONG}) is True


def test_guard_constants_are_tunable() -> None:
    assert msg_mod._CANVAS_MIN_CHARS == 20
    assert msg_mod._CANVAS_MIN_BLOCKS == 1


# ── happy path ────────────────────────────────────────────────────────────────


def test_materialize_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    db = FakeDB()
    _seed_session(db)
    run = _make_run(db)
    _patch_redis(
        monkeypatch,
        [{"kind": "article", "title": "AI 与工作", "content_markdown": _LONG, "seq": 1}],
    )

    events = _materialize(db, run)

    assert len(db.canvases) == 1
    canvas = db.canvases[0]
    assert canvas.source_run_id == "run_cvs"
    assert canvas.kind == "article"
    assert canvas.title == "AI 与工作"
    assert len(db.versions) == 1  # v1 snapshot created
    # One canvas_created SSE event with the right shape.
    assert len(events) == 1
    assert events[0].startswith("event: canvas_created\n")
    payload = json.loads(events[0].split("data: ", 1)[1].strip())
    assert payload["canvas_id"] == canvas.id
    assert payload["run_id"] == "run_cvs"


def test_materialize_backfills_message_and_event_carries_message_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = FakeDB()
    _seed_session(db)
    run = _make_run(db)
    # An assistant message for this run already exists (reconcile persisted it).
    db.messages.append(
        Message(
            id="msg_a", tenant_id=TEST_TENANT, session_id="ses_1", role="assistant",
            content="已为你创建画布。", sequence=1, run_id="run_cvs",
            canvas_id=None, created_at=_NOW,
        )
    )
    _patch_redis(
        monkeypatch,
        [{"kind": "article", "title": "T", "content_markdown": _LONG, "seq": 1}],
    )

    events = _materialize(db, run)

    assert db.messages[0].canvas_id == db.canvases[0].id  # backfilled
    payload = json.loads(events[0].split("data: ", 1)[1].strip())
    assert payload["message_id"] == "msg_a"


def test_materialize_no_payloads_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    db = FakeDB()
    _seed_session(db)
    run = _make_run(db)
    _patch_redis(monkeypatch, [])
    assert _materialize(db, run) == []
    assert db.canvases == []


def test_materialize_skipped_when_redis_url_absent() -> None:
    db = FakeDB()
    _seed_session(db)
    run = _make_run(db)
    events = _run(
        msg_mod._materialize_pending_canvases(
            run=run,
            user_id=TEST_USER,
            svc=_svc(db),
            tenant_id=TEST_TENANT,
            redis_url=None,
        )
    )
    assert events == []
    assert db.canvases == []


# ── idempotency ───────────────────────────────────────────────────────────────


def test_materialize_idempotent_across_repeated_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = FakeDB()
    _seed_session(db)
    run = _make_run(db)
    _patch_redis(
        monkeypatch,
        [{"kind": "article", "title": "Dup", "content_markdown": _LONG, "seq": 1}],
    )

    first = _materialize(db, run)   # creates
    second = _materialize(db, run)  # same payload replayed → no-op

    assert len(first) == 1
    assert second == []
    assert len(db.canvases) == 1  # created exactly once


def test_materialize_retries_message_backfill_after_canvas_won_race(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A canvas created before its assistant row must anchor on the next poll."""
    db = FakeDB()
    _seed_session(db)
    run = _make_run(db)
    _patch_redis(
        monkeypatch,
        [{"kind": "article", "title": "Race", "content_markdown": _LONG, "seq": 1}],
    )

    # First poll creates the document, but no assistant message exists yet.
    first = _materialize(db, run)
    assert len(first) == 1
    canvas_id = db.canvases[0].id

    # Reconcile persists the durable row later.  Re-reading the same Redis
    # payload must not create another canvas, but it must retry the backfill.
    db.messages.append(
        Message(
            id="msg_late",
            tenant_id=TEST_TENANT,
            session_id="ses_1",
            role="assistant",
            content="已为你创建画布。",
            sequence=1,
            run_id="run_cvs",
            canvas_id=None,
            created_at=_NOW,
        )
    )
    second = _materialize(db, run)

    assert second == []
    assert len(db.canvases) == 1
    assert db.messages[0].canvas_id == canvas_id


def test_materialize_second_payload_created_after_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = FakeDB()
    _seed_session(db)
    run = _make_run(db)

    # First poll: one payload (seq=1).
    _patch_redis(
        monkeypatch,
        [{"kind": "article", "title": "One", "content_markdown": _LONG, "seq": 1}],
    )
    _materialize(db, run)
    assert len(db.canvases) == 1

    # Second poll: the run added a second create (seq=2); seq=1 must not re-create.
    _patch_redis(
        monkeypatch,
        [
            {"kind": "article", "title": "One", "content_markdown": _LONG, "seq": 1},
            {"kind": "report", "title": "Two", "content_markdown": _LONG, "seq": 2},
        ],
    )
    events = _materialize(db, run)
    assert len(events) == 1  # only seq=2 materialized
    assert len(db.canvases) == 2
    assert db.canvases[1].title == "Two"


# ── over-trigger guard ────────────────────────────────────────────────────────


def test_materialize_skips_short_payload_no_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = FakeDB()
    _seed_session(db)
    run = _make_run(db)
    _patch_redis(
        monkeypatch,
        [{"kind": "article", "title": "Tiny", "content_markdown": "太短。", "seq": 1}],
    )

    events = _materialize(db, run)

    assert events == []         # no canvas_created emitted
    assert db.canvases == []    # nothing materialized


def test_materialize_guarded_then_valid(monkeypatch: pytest.MonkeyPatch) -> None:
    """A short seq=1 is skipped; a valid seq=2 still materializes."""
    db = FakeDB()
    _seed_session(db)
    run = _make_run(db)
    _patch_redis(
        monkeypatch,
        [
            {"kind": "article", "title": "Tiny", "content_markdown": "短", "seq": 1},
            {"kind": "article", "title": "Real", "content_markdown": _LONG, "seq": 2},
        ],
    )

    events = _materialize(db, run)
    assert len(events) == 1
    assert len(db.canvases) == 1
    assert db.canvases[0].title == "Real"


# ── fail-open ─────────────────────────────────────────────────────────────────


def test_materialize_failopen_on_service_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A materialization error is swallowed (fail-open) and returns no events —
    the reply body still flows via the message path (invariant 5)."""
    db = FakeDB()
    _seed_session(db)
    run = _make_run(db)
    _patch_redis(
        monkeypatch,
        [{"kind": "article", "title": "Boom", "content_markdown": _LONG, "seq": 1}],
    )

    # Force create_canvas to raise mid-materialization.
    from agent_session_orchestrator.services.canvas_service import CanvasService

    async def _boom(self, *a: Any, **kw: Any):  # noqa: ANN001
        raise RuntimeError("db exploded")

    monkeypatch.setattr(CanvasService, "create_canvas", _boom)

    events = _materialize(db, run)
    assert events == []  # fail-open, no crash


# ── Redis reader ──────────────────────────────────────────────────────────────


def test_read_canvas_payloads_sorts_by_seq_and_drops_non_dicts() -> None:
    from tests.test_messages_runs import _patch_redis_asyncio

    stored = json.dumps(
        [
            {"title": "B", "content_markdown": "b", "seq": 2},
            "junk",  # non-dict entries are dropped
            {"title": "A", "content_markdown": "a", "seq": 1},
        ]
    )

    class _FakeRedis:
        @classmethod
        def from_url(cls, *a: Any, **kw: Any) -> _FakeRedis:
            return cls()

        async def get(self, key: str) -> str:
            assert key == "agent:run:run_r:canvas"
            return stored

        async def aclose(self) -> None:
            return None

    with _patch_redis_asyncio(_FakeRedis):
        out = _run(
            msg_mod._read_canvas_payloads_from_redis(
                redis_url="redis://fake", run_id="run_r"
            )
        )
    assert [p["title"] for p in out] == ["A", "B"]  # sorted by seq


def test_read_canvas_payloads_empty_when_key_absent() -> None:
    from tests.test_messages_runs import _patch_redis_asyncio

    class _FakeRedis:
        @classmethod
        def from_url(cls, *a: Any, **kw: Any) -> _FakeRedis:
            return cls()

        async def get(self, key: str) -> None:
            return None

        async def aclose(self) -> None:
            return None

    with _patch_redis_asyncio(_FakeRedis):
        out = _run(
            msg_mod._read_canvas_payloads_from_redis(
                redis_url="redis://fake", run_id="run_r"
            )
        )
    assert out == []


def test_read_canvas_payloads_failopen_on_read_error() -> None:
    from tests.test_messages_runs import _patch_redis_asyncio

    class _FakeRedis:
        @classmethod
        def from_url(cls, *a: Any, **kw: Any) -> _FakeRedis:
            return cls()

        async def get(self, key: str) -> str:
            raise RuntimeError("redis down")

        async def aclose(self) -> None:
            return None

    with _patch_redis_asyncio(_FakeRedis):
        out = _run(
            msg_mod._read_canvas_payloads_from_redis(
                redis_url="redis://fake", run_id="run_r"
            )
        )
    assert out == []  # fail-open
