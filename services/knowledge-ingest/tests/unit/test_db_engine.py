"""The database engine keeps bound values out of logs.

SQL echo is opt-in (``sql_echo``) even in dev, and never shows parameters.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from knowledge_ingest.config import Settings
from knowledge_ingest.domain import db as db_module

_PG_DSN = "postgresql+asyncpg://app_user:pw@db.invalid:6432/foundation"


@pytest.fixture(autouse=True)
async def _dispose_engine() -> AsyncIterator[None]:
    yield
    await db_module.close_db()


@pytest.mark.parametrize("env", ["dev", "prod"])
async def test_sql_echo_is_off_unless_asked_for(env: str) -> None:
    await db_module.init_db(Settings(env=env, db_dsn=_PG_DSN))

    sync_engine = db_module._engine.sync_engine  # type: ignore[union-attr]
    assert sync_engine.echo is False
    assert sync_engine.hide_parameters is True


async def test_asked_for_echo_still_hides_parameters() -> None:
    await db_module.init_db(Settings(env="dev", db_dsn=_PG_DSN, sql_echo=True))

    sync_engine = db_module._engine.sync_engine  # type: ignore[union-attr]
    assert sync_engine.echo is True
    assert sync_engine.hide_parameters is True
