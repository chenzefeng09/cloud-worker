"""Per-resource ownership guards: another tenant member's session is a 404.

These tests keep the real guards on ``app_client`` (``owner_guard`` marker),
so the first ``db.execute`` a request makes is the owner lookup.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from tests._helpers import TEST_USER

pytestmark = pytest.mark.owner_guard

_PREFIX = "/api/session/v1"
_OTHER_USER = "usr_01J5TEST0000000000000USER2"


def _owner_result(owner: str | None) -> MagicMock:
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=owner)
    return result


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/sessions/ses_1"),
        ("patch", "/sessions/ses_1"),
        ("delete", "/sessions/ses_1"),
        ("get", "/sessions/ses_1/messages"),
        ("get", "/sessions/ses_1/stream"),
        ("get", "/sessions/ses_1/runs"),
        ("get", "/sessions/ses_1/runs/run_1"),
        ("post", "/sessions/ses_1/cancel"),
        ("get", "/sessions/ses_1/attachments"),
        ("get", "/sessions/ses_1/canvases"),
        ("get", "/canvases/cvs_1"),
        ("get", "/canvases/cvs_1/versions"),
    ],
)
def test_foreign_resource_is_not_found(
    app_client: TestClient, mock_db: AsyncMock, auth_headers: dict[str, str],
    method: str, path: str,
) -> None:
    mock_db.execute.return_value = _owner_result(_OTHER_USER)
    kwargs = {"json": {"title": "x"}} if method == "patch" else {}
    resp = getattr(app_client, method)(f"{_PREFIX}{path}", headers=auth_headers, **kwargs)
    assert resp.status_code == 404, resp.text


def test_missing_session_is_not_found(
    app_client: TestClient, mock_db: AsyncMock, auth_headers: dict[str, str],
) -> None:
    mock_db.execute.return_value = _owner_result(None)
    resp = app_client.get(f"{_PREFIX}/sessions/ses_gone", headers=auth_headers)
    assert resp.status_code == 404


def test_owner_passes_the_guard(
    app_client: TestClient, mock_db: AsyncMock, auth_headers: dict[str, str],
) -> None:
    session = MagicMock()
    session.configure_mock(
        id="ses_1", tenant_id="t", user_id=TEST_USER, title=None, status="ACTIVE",
        current_run_id=None, workspace_id=None, agent_metadata={},
    )
    import datetime as dt

    session.created_at = session.updated_at = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
    lookup = MagicMock()
    lookup.scalar_one_or_none = MagicMock(return_value=session)
    mock_db.execute.side_effect = [_owner_result(TEST_USER), lookup]
    resp = app_client.get(f"{_PREFIX}/sessions/ses_1", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["id"] == "ses_1"


def test_admin_replay_bypasses_owner_check(
    app_client: TestClient, mock_db: AsyncMock, auth_headers: dict[str, str],
) -> None:
    """``X-Actor: ADMIN`` (BFF-enforced tenant admin) skips the owner lookup."""
    mock_db.execute.return_value = _owner_result(None)
    resp = app_client.get(
        f"{_PREFIX}/sessions/ses_1/runs",
        headers={**auth_headers, "X-Actor": "ADMIN"},
    )
    # The only execute is the handler's own run listing (no owner lookup), so
    # a 404 here would mean the guard ran.
    assert resp.status_code == 200, resp.text
