"""The database engines keep bound values out of logs.

SQL echo is opt-in (``sql_echo``) and never shows parameters, and neither does
the message of a failed statement, which is logged with its traceback.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from agent_session_orchestrator.config import Settings
from agent_session_orchestrator.domain import db as db_module

_PG_DSN = "postgresql+asyncpg://app_user:pw@db.invalid:6432/foundation"


@pytest.fixture(autouse=True)
async def _dispose_engines() -> AsyncIterator[None]:
    yield
    await db_module.close_db()


@pytest.mark.parametrize("sql_echo", [False, True])
async def test_the_request_engine_hides_parameters(sql_echo: bool) -> None:
    await db_module.init_db(Settings(db_dsn=_PG_DSN, sql_echo=sql_echo))

    sync_engine = db_module._engine.sync_engine  # type: ignore[union-attr]
    assert sync_engine.echo is sql_echo
    assert sync_engine.hide_parameters is True


async def test_the_privileged_engine_hides_parameters(monkeypatch: pytest.MonkeyPatch) -> None:
    created: dict[str, Any] = {}

    def create_async_engine(dsn: str, **kwargs: Any) -> None:
        created.update(kwargs)
        raise RuntimeError("engine created")  # stop before it connects

    monkeypatch.setattr(db_module, "create_async_engine", create_async_engine)

    with pytest.raises(RuntimeError, match="engine created"):
        await db_module.privileged_scalars(Settings(db_dsn_migrator=_PG_DSN), "SELECT 1")
    assert created["hide_parameters"] is True
