"""Real-SQL harness for the privacy / purge tests (no Postgres needed).

The ORM models are created in an in-memory SQLite database attached as the
``session_orchestrator`` schema, with ``PRAGMA foreign_keys=ON`` so a purge
that deletes in the wrong FK order fails exactly like it would on Postgres.
:class:`AsyncSQLiteSession` adapts a sync ``Session`` to the small slice of the
``AsyncSession`` API the services use (``execute`` / ``flush`` / ``add`` /
``begin_nested`` …), so service code runs unmodified.

Postgres-only constructs (``ON CONFLICT``, the migrator scans) are covered by
compiling statements against the PostgreSQL dialect instead.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import create_engine, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session as SyncSession
from sqlalchemy.pool import StaticPool

from agent_session_orchestrator.domain.models import Base


@compiles(JSONB, "sqlite")
def _jsonb_on_sqlite(type_: Any, compiler: Any, **kw: Any) -> str:
    return "JSON"


def make_engine() -> Any:
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_conn: Any, _record: Any) -> None:
        # Let SQLAlchemy drive transactions so SAVEPOINT works (pysqlite recipe).
        dbapi_conn.isolation_level = None
        dbapi_conn.execute("ATTACH DATABASE ':memory:' AS session_orchestrator")
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    @event.listens_for(engine, "begin")
    def _on_begin(conn: Any) -> None:
        conn.exec_driver_sql("BEGIN")

    Base.metadata.create_all(engine)
    return engine


class _AsyncNested:
    def __init__(self, tx: Any) -> None:
        self._tx = tx

    async def __aenter__(self) -> Any:
        return self._tx

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        if exc_type is None:
            self._tx.commit()
        else:
            self._tx.rollback()
        return False


class AsyncSQLiteSession:
    """Async facade over a sync SQLAlchemy ``Session``."""

    def __init__(self, sync: SyncSession) -> None:
        self.sync = sync
        self.statements: list[Any] = []

    async def execute(self, stmt: Any, params: Any = None) -> Any:
        self.statements.append(stmt)
        if params is None:
            return self.sync.execute(stmt)
        return self.sync.execute(stmt, params)

    async def flush(self) -> None:
        self.sync.flush()

    async def refresh(self, obj: Any) -> None:
        self.sync.refresh(obj)

    async def delete(self, obj: Any) -> None:
        self.sync.delete(obj)

    def add(self, obj: Any) -> None:
        self.sync.add(obj)

    def begin_nested(self) -> _AsyncNested:
        return _AsyncNested(self.sync.begin_nested())


def new_db() -> tuple[AsyncSQLiteSession, SyncSession]:
    sync = SyncSession(make_engine(), expire_on_commit=False)
    return AsyncSQLiteSession(sync), sync


def ts(days_ago: float = 0) -> dt.datetime:
    return dt.datetime.now(tz=dt.UTC) - dt.timedelta(days=days_ago)


class FakeBlobStore:
    """Records deletes; ``fail_on`` keys raise like an S3 error would."""

    def __init__(self, *, fail_on: set[str] | None = None) -> None:
        self.deleted: list[str] = []
        self.fail_on = fail_on or set()

    def delete(self, key: str) -> None:
        if key in self.fail_on:
            raise RuntimeError(f"s3 down for {key}")
        self.deleted.append(key)


class RecordingHttpClient:
    """``httpx.AsyncClient`` stand-in that records every call."""

    def __init__(self, *, status: int = 200, raise_exc: Exception | None = None) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.status = status
        self.raise_exc = raise_exc

    def __call__(self, *args: Any, **kw: Any) -> RecordingHttpClient:
        return self

    async def __aenter__(self) -> RecordingHttpClient:
        return self

    async def __aexit__(self, *a: Any) -> None:
        return None

    async def _record(self, method: str, url: str, kw: dict[str, Any]) -> Any:
        import httpx

        self.calls.append((method, url, kw))
        if self.raise_exc is not None:
            raise self.raise_exc
        return httpx.Response(self.status, json={})

    async def post(self, url: str, **kw: Any) -> Any:
        return await self._record("POST", url, kw)

    async def delete(self, url: str, **kw: Any) -> Any:
        return await self._record("DELETE", url, kw)

    async def get(self, url: str, **kw: Any) -> Any:
        return await self._record("GET", url, kw)
