"""Database engine + per-request session with tenant RLS context."""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from knowledge_ingest.config import Settings

_engine = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


async def init_db(settings: Settings) -> None:
    global _engine, _session_factory
    is_sqlite = settings.db_dsn.startswith("sqlite")
    if is_sqlite:
        _engine = create_async_engine(settings.db_dsn, echo=False)
    else:
        # pgbouncer transaction-mode requires prepared_statement_cache_size=0
        url = make_url(settings.db_dsn).update_query_dict(
            {"prepared_statement_cache_size": "0"}, append=False
        )
        _engine = create_async_engine(
            url,
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            pool_recycle=settings.db_pool_recycle,
            echo=settings.env == "dev",
        )
        # ORM models are schema-less; on Postgres translate the default schema
        # (None) to this service's owned schema (SQLite tests never set this).
        _engine = _engine.execution_options(
            schema_translate_map={None: settings.db_schema}
        )
    _session_factory = async_sessionmaker(_engine, class_=AsyncSession, expire_on_commit=False)


def set_session_factory(factory: async_sessionmaker[AsyncSession] | None) -> None:
    global _session_factory
    _session_factory = factory


async def get_session(
    *,
    tenant_id: str,
    user_id: str = "SYSTEM",
    actor: str = "USER",
) -> AsyncIterator[AsyncSession]:
    assert _session_factory is not None, "DB not initialized — call init_db() first"
    if not tenant_id:
        raise ValueError("tenant_id is required to acquire a session")

    async with _session_factory() as session, session.begin():
        bind = session.get_bind()
        if bind is not None and bind.dialect.name == "postgresql":
            await session.execute(
                text(
                    "SELECT set_config('app.tenant_id', :tid, true),"
                    "       set_config('app.user_id',   :uid, true),"
                    "       set_config('app.actor',     :act, true)"
                ),
                {"tid": tenant_id, "uid": user_id, "act": actor},
            )
        yield session


async def close_db() -> None:
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _session_factory = None
