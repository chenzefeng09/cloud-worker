"""Privacy API — routing, subject resolution (self vs ADMIN), payload shapes.

The service layer is covered against real SQL in ``test_privacy_service.py``;
here the handlers run on the standard ``app_client`` (mock DB, authz allowed)
with the service methods patched where the DB would matter.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from agent_session_orchestrator.config import Settings
from agent_session_orchestrator.services.privacy_service import PrivacyService
from tests._helpers import TEST_TENANT, TEST_USER, make_scalar_result

_P = "/api/session/v1/privacy"
OTHER_USER = "usr_01J5TEST0000000000000USER2"


def _admin(headers: dict[str, str]) -> dict[str, str]:
    return {**headers, "X-Actor": "ADMIN"}


# ── subject resolution ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("method", "path", "kwargs"),
    [
        ("get", f"{_P}/export?user_id={OTHER_USER}", {}),
        ("get", f"{_P}/settings?user_id={OTHER_USER}", {}),
        ("put", f"{_P}/settings?user_id={OTHER_USER}", {"json": {"memory_enabled": False}}),
        ("post", f"{_P}/erase", {"json": {"user_id": OTHER_USER}}),
    ],
)
def test_non_admin_cannot_name_another_subject(
    app_client: TestClient, auth_headers: dict[str, str], method: str, path: str,
    kwargs: dict[str, Any],
) -> None:
    resp = getattr(app_client, method)(path, headers=auth_headers, **kwargs)
    assert resp.status_code == 403
    assert resp.json()["code"] == "AGENT.PRIVACY.FORBIDDEN"


def test_non_admin_may_name_themselves(
    app_client: TestClient, auth_headers: dict[str, str],
) -> None:
    with patch.object(
        PrivacyService, "export_user_data", AsyncMock(return_value={"ok": True}),
    ) as export:
        resp = app_client.get(f"{_P}/export?user_id={TEST_USER}", headers=auth_headers)
    assert resp.status_code == 200
    export.assert_awaited_once_with(TEST_USER)


def test_admin_exports_named_subject(
    app_client: TestClient, auth_headers: dict[str, str],
) -> None:
    with patch.object(
        PrivacyService, "export_user_data", AsyncMock(return_value={"subject_user_id": OTHER_USER}),
    ) as export:
        resp = app_client.get(
            f"{_P}/export?user_id={OTHER_USER}", headers=_admin(auth_headers),
        )
    assert resp.status_code == 200
    assert resp.json() == {"subject_user_id": OTHER_USER}
    export.assert_awaited_once_with(OTHER_USER)


def test_admin_with_malformed_subject_is_422(
    app_client: TestClient, auth_headers: dict[str, str],
) -> None:
    resp = app_client.get(f"{_P}/export?user_id=SYSTEM", headers=_admin(auth_headers))
    assert resp.status_code == 422
    assert resp.json()["code"] == "AGENT.PRIVACY.INVALID_SUBJECT"


def test_export_defaults_to_caller(
    app_client: TestClient, auth_headers: dict[str, str],
) -> None:
    with patch.object(
        PrivacyService, "export_user_data", AsyncMock(return_value={}),
    ) as export:
        assert app_client.get(f"{_P}/export", headers=auth_headers).status_code == 200
    export.assert_awaited_once_with(TEST_USER)


# ── erase ────────────────────────────────────────────────────────────────────


def test_erase_passes_dry_run_actor_and_app_dependencies(
    app_client: TestClient, auth_headers: dict[str, str],
) -> None:
    fake_store = object()
    response = {
        "subject_user_id": TEST_USER, "dry_run": True,
        "deleted": {"sessions": 1, "blobs": 0}, "runs_cancelled": 0,
    }
    with patch.object(app_client.app.state, "blob_store", fake_store, create=True), \
            patch.object(
                PrivacyService, "erase_user_data", AsyncMock(return_value=response),
            ) as erase:
        resp = app_client.post(f"{_P}/erase", headers=auth_headers, json={"dry_run": True})
    assert resp.status_code == 200
    assert resp.json() == response
    args, kwargs = erase.call_args
    assert args == (TEST_USER,)
    assert kwargs["dry_run"] is True
    assert kwargs["blob_store"] is fake_store
    assert kwargs["settings"] is app_client.app.state.settings
    assert kwargs["actor_user_id"] == TEST_USER
    assert kwargs["as_admin"] is False


def test_erase_without_body_erases_caller(
    app_client: TestClient, auth_headers: dict[str, str],
) -> None:
    with patch.object(PrivacyService, "erase_user_data", AsyncMock(return_value={})) as erase:
        assert app_client.post(f"{_P}/erase", headers=auth_headers).status_code == 200
    args, kwargs = erase.call_args
    assert args == (TEST_USER,) and kwargs["dry_run"] is False


def test_admin_erase_of_named_subject(
    app_client: TestClient, auth_headers: dict[str, str],
) -> None:
    with patch.object(PrivacyService, "erase_user_data", AsyncMock(return_value={})) as erase:
        resp = app_client.post(
            f"{_P}/erase", headers=_admin(auth_headers), json={"user_id": OTHER_USER},
        )
    assert resp.status_code == 200
    args, kwargs = erase.call_args
    assert args == (OTHER_USER,)
    assert kwargs["as_admin"] is True and kwargs["actor_user_id"] == TEST_USER


# ── settings ─────────────────────────────────────────────────────────────────


def test_get_settings_defaults_when_no_row(
    app_client: TestClient, mock_db: AsyncMock, auth_headers: dict[str, str],
) -> None:
    mock_db.execute.return_value = make_scalar_result(None)
    resp = app_client.get(f"{_P}/settings", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json() == {"memory_enabled": True, "updated_at": None}


def test_put_settings_upserts_and_echoes(
    app_client: TestClient, mock_db: AsyncMock, auth_headers: dict[str, str],
) -> None:
    result = MagicMock()
    result.one = MagicMock(return_value=(False, dt.datetime(2026, 10, 30, 9, tzinfo=dt.UTC)))
    mock_db.execute.return_value = result
    resp = app_client.put(
        f"{_P}/settings", headers=auth_headers, json={"memory_enabled": False},
    )
    assert resp.status_code == 200
    assert resp.json() == {"memory_enabled": False, "updated_at": "2026-10-30T09:00:00+00:00"}
    stmt = mock_db.execute.call_args.args[0]
    params = stmt.compile().params
    assert params["tenant_id"] == TEST_TENANT
    assert params["user_id"] == TEST_USER
    assert params["memory_enabled"] is False


def test_put_settings_validates_body(
    app_client: TestClient, auth_headers: dict[str, str],
) -> None:
    resp = app_client.put(f"{_P}/settings", headers=auth_headers, json={})
    assert resp.status_code == 422


# ── retention ────────────────────────────────────────────────────────────────


def test_retention_reports_configured_policy(
    app_client: TestClient, auth_headers: dict[str, str],
) -> None:
    resp = app_client.get(f"{_P}/retention", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json() == {"session_retention_days": 30, "trash_grace_days": 30}


def test_retention_null_when_sweep_disabled(
    app_client: TestClient, auth_headers: dict[str, str],
) -> None:
    settings: Settings = app_client.app.state.settings
    with patch.object(settings, "session_retention_days", 0), \
            patch.object(settings, "trash_grace_days", 7):
        resp = app_client.get(f"{_P}/retention", headers=auth_headers)
    assert resp.json() == {"session_retention_days": None, "trash_grace_days": 7}


# ── authz ────────────────────────────────────────────────────────────────────


def test_privacy_routes_require_rbac(
    app_client: TestClient, auth_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail-closed: a denied authz check is a 403 before any handler runs."""
    import httpx

    denied = MagicMock(spec=httpx.Response)
    denied.status_code = 200
    denied.json = MagicMock(return_value={"allowed": False})

    class _Deny:
        def __init__(self, *a: Any, **kw: Any) -> None:
            pass

        async def __aenter__(self) -> _Deny:
            return self

        async def __aexit__(self, *a: Any) -> None:
            return None

        async def post(self, *a: Any, **kw: Any) -> MagicMock:
            return denied

    monkeypatch.setattr(
        "agent_session_orchestrator.api.authz_guard.httpx.AsyncClient", _Deny,
    )
    erase = AsyncMock()
    with patch.object(PrivacyService, "erase_user_data", erase):
        for method, path in [
            ("get", f"{_P}/export"), ("post", f"{_P}/erase"), ("get", f"{_P}/settings"),
            ("get", f"{_P}/retention"),
        ]:
            assert getattr(app_client, method)(path, headers=auth_headers).status_code == 403
    erase.assert_not_awaited()
