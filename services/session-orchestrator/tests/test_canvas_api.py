"""API-layer tests for the canvas v2 endpoints.

Drives the real FastAPI routes end-to-end (routing, request/response schemas,
problem+json error mapping, retention cascade) against the in-memory
``FakeDB`` from ``test_canvas_service`` via a dependency override.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi.testclient import TestClient

from agent_session_orchestrator.config import Settings
from agent_session_orchestrator.main import create_app
from tests._helpers import TEST_TENANT, TEST_USER
from tests.test_canvas_service import FakeDB, _seed_session

_HEADERS = {"X-Tenant-Id": TEST_TENANT, "X-User-Id": TEST_USER}
_PREFIX = "/api/session/v1"


@pytest.fixture
def fake_db() -> FakeDB:
    db = FakeDB()
    _seed_session(db, "ses_1")
    return db


@pytest.fixture
def client(
    settings: Settings, fake_db: FakeDB, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    from agent_session_orchestrator.domain import db as db_module

    async def _noop(*a: Any, **k: Any) -> None:
        return None

    monkeypatch.setattr(db_module, "init_db", _noop)
    monkeypatch.setattr(db_module, "close_db", _noop)

    # authz /check always allows.
    allowed = MagicMock(spec=httpx.Response)
    allowed.status_code = 200
    allowed.json = MagicMock(return_value={"allowed": True})

    class _FakeAuthz:
        def __init__(self, *a: Any, **k: Any) -> None: ...
        async def __aenter__(self) -> _FakeAuthz:
            return self
        async def __aexit__(self, *a: Any) -> None:
            return None
        async def post(self, *a: Any, **k: Any) -> MagicMock:
            return allowed

    monkeypatch.setattr(
        "agent_session_orchestrator.api.authz_guard.httpx.AsyncClient", _FakeAuthz
    )

    app = create_app(settings)
    from agent_session_orchestrator.api.dependencies import db_session

    async def _override_db() -> AsyncIterator[FakeDB]:
        yield fake_db

    app.dependency_overrides[db_session] = _override_db
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


# ── create / get / list ──────────────────────────────────────────────────────


def test_create_canvas_from_content(client: TestClient) -> None:
    resp = client.post(
        f"{_PREFIX}/sessions/ses_1/canvases",
        headers=_HEADERS,
        json={"content": "# 标题\n\n正文段落。", "kind": "article"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["kind"] == "article"
    assert body["current_version"] == 1
    assert body["session_id"] == "ses_1"
    assert [b["text"] for b in body["blocks"]] == ["# 标题", "正文段落。"]
    assert body["blocks"][0]["type"] == "heading"


def test_create_canvas_on_soft_deleted_session_404(
    client: TestClient, fake_db: FakeDB
) -> None:
    fake_db.sessions[0].deleted_at = __import__("datetime").datetime.now(
        __import__("datetime").UTC
    )
    resp = client.post(
        f"{_PREFIX}/sessions/ses_1/canvases",
        headers=_HEADERS,
        json={"content": "x"},
    )
    assert resp.status_code == 404
    assert resp.json()["code"] == "AGENT.SESSION.NOT_FOUND"


def test_get_and_list_canvas(client: TestClient) -> None:
    created = client.post(
        f"{_PREFIX}/sessions/ses_1/canvases",
        headers=_HEADERS,
        json={"content": "hello body"},
    ).json()
    cid = created["id"]

    got = client.get(f"{_PREFIX}/canvases/{cid}", headers=_HEADERS)
    assert got.status_code == 200
    assert got.json()["id"] == cid

    listed = client.get(f"{_PREFIX}/sessions/ses_1/canvases", headers=_HEADERS)
    assert listed.status_code == 200
    items = listed.json()["items"]
    assert len(items) == 1
    assert items[0]["id"] == cid
    # List omits blocks (summary shape).
    assert "blocks" not in items[0]


def test_get_missing_canvas_404(client: TestClient) -> None:
    resp = client.get(f"{_PREFIX}/canvases/cvs_missing", headers=_HEADERS)
    assert resp.status_code == 404
    body = resp.json()
    assert body["code"] == "AGENT.SESSION.CANVAS.NOT_FOUND"
    assert body["user_message"]  # English user_message present


# ── versions + optimistic lock ───────────────────────────────────────────────


def test_manual_version_and_conflict(client: TestClient) -> None:
    cid = client.post(
        f"{_PREFIX}/sessions/ses_1/canvases",
        headers=_HEADERS,
        json={"content": "v1"},
    ).json()["id"]

    ok = client.post(
        f"{_PREFIX}/canvases/{cid}/versions",
        headers=_HEADERS,
        json={
            "blocks": [{"id": "blk_a", "type": "paragraph", "text": "edited"}],
            "base_version": 1,
        },
    )
    assert ok.status_code == 201, ok.text
    assert ok.json()["current_version"] == 2

    stale = client.post(
        f"{_PREFIX}/canvases/{cid}/versions",
        headers=_HEADERS,
        json={"blocks": [], "base_version": 1},  # stale
    )
    assert stale.status_code == 409
    assert stale.json()["code"] == "AGENT.SESSION.CANVAS.VERSION_CONFLICT"


def test_version_list_and_get(client: TestClient) -> None:
    cid = client.post(
        f"{_PREFIX}/sessions/ses_1/canvases",
        headers=_HEADERS,
        json={"content": "v1"},
    ).json()["id"]
    client.post(
        f"{_PREFIX}/canvases/{cid}/versions",
        headers=_HEADERS,
        json={
            "blocks": [{"id": "blk_a", "type": "paragraph", "text": "v2"}],
            "base_version": 1,
        },
    )
    versions = client.get(f"{_PREFIX}/canvases/{cid}/versions", headers=_HEADERS)
    assert versions.status_code == 200
    items = versions.json()["items"]
    assert [v["version"] for v in items] == [1, 2]
    assert "blocks" not in items[0]  # summary omits blocks

    v2 = client.get(f"{_PREFIX}/canvases/{cid}/versions/2", headers=_HEADERS)
    assert v2.status_code == 200
    assert v2.json()["blocks"][0]["text"] == "v2"

    missing = client.get(f"{_PREFIX}/canvases/{cid}/versions/99", headers=_HEADERS)
    assert missing.status_code == 404
    assert missing.json()["code"] == "AGENT.SESSION.CANVAS.VERSION_NOT_FOUND"


# ── suggestions: create / accept / reject ────────────────────────────────────


def _create_canvas_with_blocks(client: TestClient) -> tuple[str, list[dict]]:
    canvas = client.post(
        f"{_PREFIX}/sessions/ses_1/canvases",
        headers=_HEADERS,
        json={"content": "first para\n\nsecond para"},
    ).json()
    return canvas["id"], canvas["blocks"]


def test_suggestion_create_and_accept(client: TestClient) -> None:
    cid, blocks = _create_canvas_with_blocks(client)
    target = blocks[0]["id"]

    sug = client.post(
        f"{_PREFIX}/canvases/{cid}/suggestions",
        headers=_HEADERS,
        json={
            "base_version": 1,
            "instruction": "改写第一段",
            "diffs": [{"block_id": target, "op": "replace", "sugg": "NEW FIRST"}],
            "change_summary": "润色",
        },
    )
    assert sug.status_code == 201, sug.text
    sug_body = sug.json()
    assert sug_body["status"] == "proposed"
    sgid = sug_body["id"]

    accepted = client.post(
        f"{_PREFIX}/canvases/{cid}/suggestions/{sgid}/accept", headers=_HEADERS
    )
    assert accepted.status_code == 200, accepted.text
    acc_body = accepted.json()
    assert acc_body["current_version"] == 2
    assert acc_body["blocks"][0]["text"] == "NEW FIRST"
    assert acc_body["blocks"][0]["id"] == target  # id stability


def test_accept_conflict_after_intervening_edit(client: TestClient) -> None:
    cid, blocks = _create_canvas_with_blocks(client)
    target = blocks[0]["id"]
    sgid = client.post(
        f"{_PREFIX}/canvases/{cid}/suggestions",
        headers=_HEADERS,
        json={
            "base_version": 1,
            "instruction": "x",
            "diffs": [{"block_id": target, "op": "replace", "sugg": "z"}],
        },
    ).json()["id"]
    # Intervening manual edit bumps current_version to 2.
    client.post(
        f"{_PREFIX}/canvases/{cid}/versions",
        headers=_HEADERS,
        json={
            "blocks": [{"id": "blk_x", "type": "paragraph", "text": "hand"}],
            "base_version": 1,
        },
    )
    # Now accepting the base_version=1 suggestion must 409.
    resp = client.post(
        f"{_PREFIX}/canvases/{cid}/suggestions/{sgid}/accept", headers=_HEADERS
    )
    assert resp.status_code == 409
    assert resp.json()["code"] == "AGENT.SESSION.CANVAS.VERSION_CONFLICT"


def test_reject_then_accept_is_resolved_conflict(client: TestClient) -> None:
    cid, blocks = _create_canvas_with_blocks(client)
    sgid = client.post(
        f"{_PREFIX}/canvases/{cid}/suggestions",
        headers=_HEADERS,
        json={"base_version": 1, "instruction": "x", "diffs": []},
    ).json()["id"]

    rej = client.post(
        f"{_PREFIX}/canvases/{cid}/suggestions/{sgid}/reject", headers=_HEADERS
    )
    assert rej.status_code == 200
    assert rej.json()["status"] == "rejected"

    # Accepting an already-rejected suggestion → SUGGESTION_RESOLVED 409.
    acc = client.post(
        f"{_PREFIX}/canvases/{cid}/suggestions/{sgid}/accept", headers=_HEADERS
    )
    assert acc.status_code == 409
    assert acc.json()["code"] == "AGENT.SESSION.CANVAS.SUGGESTION_RESOLVED"


# ── revert ───────────────────────────────────────────────────────────────────


def test_revert_creates_new_version(client: TestClient) -> None:
    cid = client.post(
        f"{_PREFIX}/sessions/ses_1/canvases",
        headers=_HEADERS,
        json={"content": "original"},
    ).json()["id"]
    client.post(
        f"{_PREFIX}/canvases/{cid}/versions",
        headers=_HEADERS,
        json={
            "blocks": [{"id": "blk_x", "type": "paragraph", "text": "changed"}],
            "base_version": 1,
        },
    )
    reverted = client.post(
        f"{_PREFIX}/canvases/{cid}/revert",
        headers=_HEADERS,
        json={"to_version": 1},
    )
    assert reverted.status_code == 201, reverted.text
    body = reverted.json()
    assert body["current_version"] == 3
    assert body["blocks"][0]["text"] == "original"


# ── cross-tenant isolation (application-level tenant predicate) ───────────────


def test_cross_tenant_canvas_invisible(client: TestClient) -> None:
    cid = client.post(
        f"{_PREFIX}/sessions/ses_1/canvases",
        headers=_HEADERS,
        json={"content": "secret"},
    ).json()["id"]
    # A different tenant's request must not see this canvas.  The FakeDB honours
    # the tenant_id predicate the service adds (RLS is the DB-level backstop in
    # prod, exercised by live_db tests).
    other = {"X-Tenant-Id": "tnt_OTHER0000000000000000000", "X-User-Id": TEST_USER}
    resp = client.get(f"{_PREFIX}/canvases/{cid}", headers=other)
    assert resp.status_code == 404
