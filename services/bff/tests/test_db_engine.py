"""The BFF database engine keeps bound values out of logs and error messages.

The BFF's tables hold encrypted session tokens, API key hashes and personal
data.  SQL echo is opt-in (``sql_echo``) and never shows parameters; a DBAPI
error's message does not carry them either.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from agent_bff.config import Settings
from agent_bff.domain import db as db_module

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


async def test_a_failed_statement_does_not_carry_its_parameters() -> None:
    await db_module.init_db(Settings(env="test", db_dsn="sqlite+aiosqlite:///:memory:"))

    with pytest.raises(DBAPIError) as caught:
        async with db_module._engine.connect() as conn:  # type: ignore[union-attr]
            await conn.execute(
                text("SELECT * FROM no_such_table WHERE token = :token"),
                {"token": "gAAAAA-session-token-ciphertext"},
            )

    assert "gAAAAA-session-token-ciphertext" not in str(caught.value)
    assert "hide_parameters" in str(caught.value)
