"""HTTP-layer tests for session-orchestrator using mocked DB."""

from __future__ import annotations

from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from agent_session_orchestrator.domain.models import Session
from tests._helpers import TEST_TENANT, TEST_USER, make_scalar_result


def test_healthz(app_client: TestClient) -> None:
    resp = app_client.get("/api/session/v1/healthz")
    assert resp.status_code == 200


def test_list_sessions_requires_tenant(app_client: TestClient) -> None:
    resp = app_client.get("/api/session/v1/sessions")
    assert resp.status_code == 401


def test_list_sessions_returns_empty(
    app_client: TestClient, mock_db: AsyncMock, auth_headers,
) -> None:
    mock_db.execute.return_value = make_scalar_result([])
    resp = app_client.get("/api/session/v1/sessions", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["items"] == []


def test_create_session_populates_tenant_from_header(
    app_client: TestClient, mock_db: AsyncMock, auth_headers,
) -> None:
    # SessionService.create_session does add → flush → refresh.  The mock
    # refresh is a no-op, so we patch it to populate the server-default
    # fields that pydantic SessionOut needs.
    import datetime as dt

    async def _refresh(obj: Session) -> None:
        obj.id = obj.id or "ses_01J5MOCK00000000000000NEW1"
        obj.status = obj.status or "ACTIVE"
        obj.created_at = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
        obj.updated_at = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)

    mock_db.refresh.side_effect = _refresh

    resp = app_client.post(
        "/api/session/v1/sessions",
        headers=auth_headers,
        json={"title": "demo"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["tenant_id"] == TEST_TENANT
    assert body["user_id"] == TEST_USER
    assert body["title"] == "demo"
    # And confirm the service really created a session with the right tenant.
    added: Session = mock_db.add.call_args.args[0]
    assert added.tenant_id == TEST_TENANT


def test_get_session_404_when_missing(
    app_client: TestClient, mock_db: AsyncMock, auth_headers,
) -> None:
    mock_db.execute.return_value = make_scalar_result(None)
    resp = app_client.get(
        "/api/session/v1/sessions/ses_unknown", headers=auth_headers,
    )
    assert resp.status_code == 404


def test_delete_session_returns_204(
    app_client: TestClient, mock_db: AsyncMock, auth_headers,
) -> None:
    fake = Session(
        id="ses_1", tenant_id=TEST_TENANT, user_id=TEST_USER, status="ACTIVE",
    )
    mock_db.execute.return_value = make_scalar_result(fake)
    resp = app_client.delete(
        "/api/session/v1/sessions/ses_1", headers=auth_headers,
    )
    assert resp.status_code == 204
