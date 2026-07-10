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
    # The dev/CI deploy fronts Postgres with pgbouncer in ``transaction``
    # pool mode, which rebinds server connections per transaction and
    # therefore cannot honour asyncpg's per-connection prepared-statement
    # cache.  Both caches must be disabled to avoid
    # ``DuplicatePreparedStatementError``:
    #   * ``statement_cache_size=0``           -> asyncpg's own LRU
    #   * ``prepared_statement_cache_size=0``  -> SQLAlchemy dialect cache
    # Per SQLAlchemy docs the dialect arg goes in the URL query string,
    # not in ``connect_args`` (asyncpg's ``connect`` would error).
    dsn = settings.db_dsn
    sep = "&" if "?" in dsn else "?"
    if "prepared_statement_cache_size" not in dsn:
        dsn = f"{dsn}{sep}prepared_statement_cache_size=0"
    _engine = create_async_engine(
        dsn,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_recycle=settings.db_pool_recycle,
        # Off by default (see Settings.sql_echo).  Previously ``env == "dev"``,
        # which turned the engine echo firehose on for every dev deploy and
        # could back up the log pipe enough to stall the SSE event loop.
        echo=settings.sql_echo,
        connect_args={"statement_cache_size": 0},
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


async def privileged_scalars(settings: Settings, sql: str, params: dict | None = None) -> list:
    """Run a read-only query on the migrator (BYPASSRLS) connection.

    Background jobs (e.g. the session-retention sweep) have no request and
    therefore no single ``app.tenant_id`` to bind, so they cannot use
    :func:`get_session`: the RLS policies installed by ``0001_init`` would
    filter every row out.  The ``app_migrator`` role retains BYPASSRLS for
    exactly this kind of operational, cross-tenant read (see the migration
    docstring).

    This helper opens a short-lived engine on ``db_dsn_migrator``, runs the
    query, and disposes immediately — it is **not** pooled because the sweep
    runs at most once a day.  Use it only for cross-tenant *reads*; the
    actual writes (soft-deletes) go through the normal tenant-bound
    ``app_user`` path so RLS still guards every mutation.

    Returns the first column of every row (``Result.scalars().all()``).
    """
    dsn = settings.db_dsn_migrator
    sep = "&" if "?" in dsn else "?"
    if "prepared_statement_cache_size" not in dsn:
        dsn = f"{dsn}{sep}prepared_statement_cache_size=0"
    engine = create_async_engine(dsn, connect_args={"statement_cache_size": 0})
    try:
        async with engine.connect() as conn:
            result = await conn.execute(text(sql), params or {})
            return list(result.scalars().all())
    finally:
        await engine.dispose()
