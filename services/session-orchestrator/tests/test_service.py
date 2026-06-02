"""SessionService unit tests — pure logic, mocked DB."""

from __future__ import annotations

import datetime as dt
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent_session_orchestrator.domain.models import Message, Session
from agent_session_orchestrator.errors import RunNotFoundError, SessionNotFoundError
from agent_session_orchestrator.services.session_service import SessionService
from tests._helpers import TEST_TENANT, make_scalar_result


@pytest.fixture
def svc(mock_db: AsyncMock) -> SessionService:
    return SessionService(mock_db, tenant_id=TEST_TENANT)


@pytest.mark.asyncio
async def test_create_session_persists_and_refreshes(
    svc: SessionService, mock_db: AsyncMock,
) -> None:
    session = await svc.create_session(user_id="u1", title="hi")
    assert session.tenant_id == TEST_TENANT
    assert session.user_id == "u1"
    mock_db.add.assert_called_once()
    mock_db.flush.assert_awaited_once()
    mock_db.refresh.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_session_raises_when_missing(
    svc: SessionService, mock_db: AsyncMock,
) -> None:
    mock_db.execute.return_value = make_scalar_result(None)
    with pytest.raises(SessionNotFoundError):
        await svc.get_session("ses_unknown")


@pytest.mark.asyncio
async def test_get_session_returns_row(
    svc: SessionService, mock_db: AsyncMock,
) -> None:
    fake = Session(
        id="ses_1", tenant_id=TEST_TENANT, user_id="u1", status="ACTIVE",
    )
    mock_db.execute.return_value = make_scalar_result(fake)
    found = await svc.get_session("ses_1")
    assert found.id == "ses_1"


@pytest.mark.asyncio
async def test_list_messages_paginates(
    svc: SessionService, mock_db: AsyncMock,
) -> None:
    # First execute call is the inner get_session (Session row),
    # second is the actual list.
    session_row = Session(
        id="ses_1", tenant_id=TEST_TENANT, user_id="u1", status="ACTIVE",
    )
    msgs = [
        Message(
            id=f"msg_{i}", tenant_id=TEST_TENANT, session_id="ses_1",
            role="USER", content=f"m{i}", sequence=i + 1,
            created_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
        )
        for i in range(3)
    ]
    mock_db.execute.side_effect = [
        make_scalar_result(session_row),
        make_scalar_result(msgs),
    ]
    rows, next_cursor = await svc.list_messages("ses_1", limit=50)
    assert len(rows) == 3
    assert next_cursor is None


@pytest.mark.asyncio
async def test_list_messages_rejects_non_int_cursor(
    svc: SessionService, mock_db: AsyncMock,
) -> None:
    session_row = Session(
        id="ses_1", tenant_id=TEST_TENANT, user_id="u1", status="ACTIVE",
    )
    mock_db.execute.return_value = make_scalar_result(session_row)
    with pytest.raises(ValueError, match="cursor must be an int"):
        await svc.list_messages("ses_1", cursor="abc")


@pytest.mark.asyncio
async def test_get_run_raises_when_missing(
    svc: SessionService, mock_db: AsyncMock,
) -> None:
    mock_db.execute.return_value = make_scalar_result(None)
    with pytest.raises(RunNotFoundError):
        await svc.get_run("ses_1", "run_unknown")


@pytest.mark.asyncio
async def test_delete_session_marks_deleted_at(
    svc: SessionService, mock_db: AsyncMock,
) -> None:
    fake = Session(
        id="ses_1", tenant_id=TEST_TENANT, user_id="u1", status="ACTIVE",
    )
    mock_db.execute.return_value = make_scalar_result(fake)
    await svc.delete_session("ses_1")
    assert fake.deleted_at is not None
    mock_db.flush.assert_awaited()


@pytest.mark.asyncio
async def test_create_message_increments_sequence(
    svc: SessionService, mock_db: AsyncMock,
) -> None:
    """create_message issues two execute calls: get_session, then max(sequence)."""
    session_row = Session(
        id="ses_1", tenant_id=TEST_TENANT, user_id="u1", status="ACTIVE",
    )
    seq_result = MagicMock()
    seq_result.scalar_one_or_none = MagicMock(return_value=7)
    mock_db.execute.side_effect = [
        make_scalar_result(session_row),
        seq_result,
    ]
    msg = await svc.create_message(
        "ses_1", role="USER", content="hi", tool_calls=None, run_id=None,
    )
    assert msg.sequence == 8
