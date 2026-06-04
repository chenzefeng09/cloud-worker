"""Tests for attachment upload/listing endpoints."""

from __future__ import annotations

import datetime as dt
import io
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from agent_session_orchestrator.domain.models import Attachment, Session
from tests._helpers import TEST_TENANT, TEST_USER, make_scalar_result

_NOW = dt.datetime(2026, 6, 3, 0, 0, 0, tzinfo=dt.UTC)


def _fake_session(session_id: str = "ses_001") -> Session:
    return Session(
        id=session_id,
        tenant_id=TEST_TENANT,
        user_id=TEST_USER,
        status="ACTIVE",
        created_at=_NOW,
        updated_at=_NOW,
    )


def _fake_attachment(att_id: str = "att_001", session_id: str = "ses_001") -> Attachment:
    return Attachment(
        id=att_id,
        tenant_id=TEST_TENANT,
        session_id=session_id,
        user_id=TEST_USER,
        filename="hello.txt",
        mime="text/plain",
        size_bytes=5,
        blob_key=f"tenant/{TEST_TENANT}/sessions/{session_id}/attachments/{att_id}/hello.txt",
        created_at=_NOW,
    )


def test_list_attachments_empty(
    app_client: TestClient, mock_db: AsyncMock, auth_headers: dict,
) -> None:
    """GET /attachments returns empty list when no attachments exist."""
    session = _fake_session()
    # First execute → get_session; second execute → list_attachments
    mock_db.execute.side_effect = [
        make_scalar_result(session),
        make_scalar_result([]),
    ]
    resp = app_client.get(
        "/api/session/v1/sessions/ses_001/attachments",
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert resp.json() == {"items": []}


def test_list_attachments_returns_rows(
    app_client: TestClient, mock_db: AsyncMock, auth_headers: dict,
) -> None:
    session = _fake_session()
    att = _fake_attachment()
    mock_db.execute.side_effect = [
        make_scalar_result(session),
        make_scalar_result([att]),
    ]
    resp = app_client.get(
        "/api/session/v1/sessions/ses_001/attachments",
        headers=auth_headers,
    )
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["id"] == "att_001"
    assert items[0]["filename"] == "hello.txt"


def test_upload_attachment(
    app_client: TestClient, mock_db: AsyncMock, auth_headers: dict,
) -> None:
    """POST /attachments stores blob and returns attachment metadata."""
    session = _fake_session()

    async def _refresh(obj: Attachment) -> None:
        obj.id = "att_001"
        obj.created_at = _NOW

    mock_db.execute.return_value = make_scalar_result(session)
    mock_db.refresh.side_effect = _refresh

    # Patch the blob_store on the app state so we don't need real S3.
    fake_blob = MagicMock()
    fake_blob.put = MagicMock(return_value="s3://agent-platform/key")

    with patch.object(
        app_client.app.state,
        "blob_store",
        fake_blob,
        create=True,
    ):
        resp = app_client.post(
            "/api/session/v1/sessions/ses_001/attachments",
            headers=auth_headers,
            files={"file": ("hello.txt", io.BytesIO(b"hello"), "text/plain")},
        )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["filename"] == "hello.txt"
    assert body["mime"] == "text/plain"
    assert body["size_bytes"] == 5
    # Verify blob.put was called with the right data
    fake_blob.put.assert_called_once()
    call_args = fake_blob.put.call_args
    assert call_args[0][1] == b"hello"
