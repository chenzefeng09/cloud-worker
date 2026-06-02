"""Database engine and per-request session factory.

Tenant isolation contract
-------------------------
Every session yielded by :func:`get_session` is opened inside a transaction
that has already executed::

    SELECT set_config('app.tenant_id', <tenant>, true),
           set_config('app.user_id',   <user>,   true),
           set_config('app.actor',     'USER',   true);

Without those ``set_config`` calls the RLS policies installed by migration
``0001_init`` evaluate ``tenant_id = NULL`` which is always false, so every
query would silently return zero rows.  Forgetting to set them is the most
common B-layer mistake (see ``saas-foundation/docs/multi-tenancy.md §5``).

The session is bound to a single transaction on purpose: ``SET LOCAL`` is
released on ``COMMIT`` / ``ROLLBACK``, so reusing a connection across
transactions without re-applying the settings would let it leak tenant data.
``get_session`` therefore opens ``session.begin()`` itself and the caller
must let it close cleanly — do **not** keep a session open across multiple
requests or long-running SSE loops.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from agent_session_orchestrator.config import Settings

_engine = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


async def init_db(settings: Settings) -> None:
    global _engine, _session_factory
    _engine = create_async_engine(
        settings.db_dsn,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_recycle=settings.db_pool_recycle,
        echo=settings.env == "dev",
    )
    _session_factory = async_sessionmaker(_engine, class_=AsyncSession, expire_on_commit=False)


async def get_session(
    *,
    tenant_id: str,
    user_id: str = "SYSTEM",
    actor: str = "USER",
) -> AsyncIterator[AsyncSession]:
    """Acquire a session whose transaction is bound to the given tenant.

    Args:
        tenant_id: MUST be non-empty.  The caller (a FastAPI dependency)
            is responsible for refusing the request with 401 when the
            tenant header is absent — see ``api/dependencies.py``.
        user_id: User responsible for the request, or ``"SYSTEM"`` for
            internal jobs that legitimately need cross-user access.
        actor: One of ``USER`` / ``SYSTEM`` / ``MIGRATOR`` / ``ADMIN`` —
            stored in the audit log for forensic queries.

    Raises:
        AssertionError: ``init_db`` was not called yet (programming error).
        ValueError: ``tenant_id`` is empty — never silently widen to NULL.
    """
    assert _session_factory is not None, "DB not initialized — call init_db() first"
    if not tenant_id:
        raise ValueError("tenant_id is required to acquire a session")

    async with _session_factory() as session, session.begin():
            # set_config(name, value, is_local=true) is the parameterised
            # equivalent of ``SET LOCAL`` — preferred so the tenant id is
            # never spliced into SQL via string formatting.
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
    global _engine
    if _engine is not None:
        await _engine.dispose()
        _engine = None
