"""Tests for the background reconcile sweeper.

Focus: the soft-deleted-session orphan-run path.  When the 30-day retention
sweep soft-deletes a session (``deleted_at`` set, row still present), any of
its runs still in a non-terminal state become orphans.  The RLS-bound
``get_session`` filters ``deleted_at IS NULL`` and therefore can't resolve the
owner's ``user_id`` — historically the sweeper fell back to ``SYSTEM``, which
the scheduler rejects with 403, wedging the run in RUNNING forever.

The fix carries ``user_id`` out of the cross-tenant scan (a LEFT JOIN on the
session table that sees soft-deleted rows), so ``_reconcile_run_completion``
always receives the session's real ``user_id`` — never ``SYSTEM`` — for these
orphaned runs.

These tests drive ``_sweep_once`` directly with the module-level collaborators
(``privileged_scalars``, ``acquire_db_session``, ``SessionService``,
``_reconcile_run_completion``) monkeypatched, so no Postgres is required.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

import agent_session_orchestrator.services.reconcile_sweeper as sweeper
from agent_session_orchestrator.config import Settings
from agent_session_orchestrator.errors import SessionNotFoundError
from tests._helpers import TEST_TENANT, TEST_USER

RUN_ID = "run_01J5TEST00000000000000RUN01"
SESSION_ID = "ses_01J5TEST0000000000000SES01"


def _make_app(settings: Settings) -> MagicMock:
    app = MagicMock()
    app.state.settings = settings
    return app


def _scan_row(tenant_id: str, run_id: str, session_id: str, user_id: str) -> str:
    """Encode a scan row exactly as the LEFT-JOIN SQL does."""
    return "\x1f".join([tenant_id, run_id, session_id, user_id])


class _FakeSvc:
    """Minimal SessionService stand-in for the sweeper's per-run block.

    ``get_session`` raises when ``soft_deleted`` (mirroring the RLS
    ``deleted_at IS NULL`` filter); ``list_runs`` returns the orphaned run
    regardless (the real ``list_runs`` doesn't filter on the session).
    """

    def __init__(self, *, soft_deleted: bool, run_obj: Any) -> None:
        self._soft_deleted = soft_deleted
        self._run_obj = run_obj

    async def get_session(self, session_id: str) -> Any:
        if self._soft_deleted:
            raise SessionNotFoundError(session_id)
        obj = MagicMock()
        obj.user_id = TEST_USER
        return obj

    async def list_runs(self, session_id: str) -> list[Any]:
        return [self._run_obj]


@pytest.fixture
def patched_sweeper(
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, Any]:
    """Patch the sweeper's collaborators and capture the reconcile call.

    Returns a dict of handles the test can configure/inspect:
    * ``set_scan_rows(rows)`` — what privileged_scalars yields.
    * ``set_svc(svc)`` — the SessionService instance used per run.
    * ``reconcile`` — AsyncMock recording the reconcile invocation.
    * ``run_obj`` — the fake Run returned by list_runs.
    """
    run_obj = MagicMock()
    run_obj.id = RUN_ID
    run_obj.status = "RUNNING"

    state: dict[str, Any] = {"rows": [], "svc": None}

    async def _fake_privileged_scalars(
        settings: Settings, sql: str, params: dict | None = None
    ) -> list:
        # Assert the fix is actually in the SQL: LEFT JOIN + user_id projected.
        assert "LEFT JOIN session_orchestrator.session" in sql
        assert "COALESCE(s.user_id, '')" in sql
        return list(state["rows"])

    async def _fake_acquire_db_session(
        *, tenant_id: str, user_id: str, actor: str
    ) -> AsyncIterator[Any]:
        yield MagicMock()  # db handle — unused, SessionService is patched

    def _fake_session_service(db: Any, *, tenant_id: str) -> Any:
        assert state["svc"] is not None, "test must call set_svc() first"
        return state["svc"]

    reconcile = AsyncMock(return_value=(True, []))

    monkeypatch.setattr(sweeper, "privileged_scalars", _fake_privileged_scalars)
    monkeypatch.setattr(sweeper, "acquire_db_session", _fake_acquire_db_session)
    monkeypatch.setattr(sweeper, "SessionService", _fake_session_service)
    # _reconcile_run_completion is imported lazily inside _sweep_once from the
    # messages module, so patch it at its definition site.
    monkeypatch.setattr(
        "agent_session_orchestrator.api.v1.messages._reconcile_run_completion",
        reconcile,
    )

    def set_scan_rows(rows: list[str]) -> None:
        state["rows"] = rows

    def set_svc(svc: Any) -> None:
        state["svc"] = svc

    return {
        "set_scan_rows": set_scan_rows,
        "set_svc": set_svc,
        "reconcile": reconcile,
        "run_obj": run_obj,
    }


@pytest.fixture
def sweeper_settings() -> Settings:
    return Settings(env="test", log_level="WARNING", otel_enabled=False)


async def test_soft_deleted_session_reconciles_with_real_user_id(
    patched_sweeper: dict[str, Any],
    sweeper_settings: Settings,
) -> None:
    """Soft-deleted session: reconcile gets the real user_id, never SYSTEM.

    The scan carries the owner's user_id (LEFT JOIN sees the soft-deleted
    row); get_session raises (RLS-scoped, filters deleted_at) but must NOT
    change the user_id passed downstream.
    """
    run_obj = patched_sweeper["run_obj"]
    patched_sweeper["set_scan_rows"](
        [_scan_row(TEST_TENANT, RUN_ID, SESSION_ID, TEST_USER)]
    )
    patched_sweeper["set_svc"](_FakeSvc(soft_deleted=True, run_obj=run_obj))

    app = _make_app(sweeper_settings)
    await sweeper._sweep_once(app)

    reconcile = patched_sweeper["reconcile"]
    reconcile.assert_awaited_once()
    kwargs = reconcile.await_args.kwargs
    assert kwargs["user_id"] == TEST_USER
    assert kwargs["user_id"] != "SYSTEM"
    assert kwargs["tenant_id"] == TEST_TENANT
    assert kwargs["run"] is run_obj


async def test_live_session_also_uses_scan_user_id(
    patched_sweeper: dict[str, Any],
    sweeper_settings: Settings,
) -> None:
    """Ordinary (not soft-deleted) session still reconciles with real user_id."""
    run_obj = patched_sweeper["run_obj"]
    patched_sweeper["set_scan_rows"](
        [_scan_row(TEST_TENANT, RUN_ID, SESSION_ID, TEST_USER)]
    )
    patched_sweeper["set_svc"](_FakeSvc(soft_deleted=False, run_obj=run_obj))

    app = _make_app(sweeper_settings)
    await sweeper._sweep_once(app)

    kwargs = patched_sweeper["reconcile"].await_args.kwargs
    assert kwargs["user_id"] == TEST_USER


async def test_empty_scan_user_id_falls_back_without_crashing(
    patched_sweeper: dict[str, Any],
    sweeper_settings: Settings,
) -> None:
    """Empty scan user_id (session row genuinely gone) → SYSTEM fallback, no crash.

    COALESCE yields '' when there is no matching session row.  get_session then
    also fails, so the sweeper falls back to the historical SYSTEM sentinel
    rather than raising.
    """
    run_obj = patched_sweeper["run_obj"]
    patched_sweeper["set_scan_rows"](
        [_scan_row(TEST_TENANT, RUN_ID, SESSION_ID, "")]  # empty user_id
    )
    patched_sweeper["set_svc"](_FakeSvc(soft_deleted=True, run_obj=run_obj))

    app = _make_app(sweeper_settings)
    # Must not raise.
    await sweeper._sweep_once(app)

    kwargs = patched_sweeper["reconcile"].await_args.kwargs
    assert kwargs["user_id"] == "SYSTEM"


async def test_no_stale_runs_is_a_noop(
    patched_sweeper: dict[str, Any],
    sweeper_settings: Settings,
) -> None:
    """No scan rows → reconcile never called."""
    patched_sweeper["set_scan_rows"]([])
    patched_sweeper["set_svc"](
        _FakeSvc(soft_deleted=False, run_obj=patched_sweeper["run_obj"])
    )

    app = _make_app(sweeper_settings)
    await sweeper._sweep_once(app)

    patched_sweeper["reconcile"].assert_not_awaited()
