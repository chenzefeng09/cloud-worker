"""Search against a real PostgreSQL + pgvector database.

Opt-in: these tests run only when ``KNOWLEDGE_INGEST_TEST_PG_DSN`` is an
asyncpg DSN for a database migrated with this service's alembic revisions
(``vector`` extension installed, ``chunks.embedding`` a ``vector(1536)``)::

    KNOWLEDGE_INGEST_TEST_PG_DSN=postgresql+asyncpg://app_user:…@localhost:5432/foundation \\
        poetry run pytest tests/integration

``KNOWLEDGE_INGEST_TEST_PG_SCHEMA`` names the service schema (default
``knowledge_ingest``).  Each test uses a fresh random tenant id and runs in one
transaction that is rolled back at the end, so it commits nothing.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from typing import Any

import numpy as np
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from knowledge_ingest.blob.memory import InMemoryBlobStore
from knowledge_ingest.services.ingest_service import IngestService
from tests.conftest import FakeEmbeddings

PG_DSN = os.environ.get("KNOWLEDGE_INGEST_TEST_PG_DSN", "")
PG_SCHEMA = os.environ.get("KNOWLEDGE_INGEST_TEST_PG_SCHEMA", "knowledge_ingest")

pytestmark = pytest.mark.skipif(
    not PG_DSN, reason="set KNOWLEDGE_INGEST_TEST_PG_DSN to run against Postgres + pgvector",
)

USER_A = "usr_ITUSERA" + "0" * 19
USER_B = "usr_ITUSERB" + "0" * 19


@pytest.fixture
def tenant_id() -> str:
    return f"tnt_it_{uuid.uuid4().hex[:24]}"


@pytest_asyncio.fixture
async def pg_session(tenant_id: str) -> AsyncIterator[AsyncSession]:
    # Same engine options as ``domain.db.init_db`` on Postgres.
    url = make_url(PG_DSN).update_query_dict(
        {"prepared_statement_cache_size": "0"}, append=False,
    )
    engine = create_async_engine(url, poolclass=NullPool).execution_options(
        schema_translate_map={None: PG_SCHEMA},
    )
    try:
        async with engine.connect() as conn:
            outer = await conn.begin()
            try:
                # The RLS context ``domain.db.get_session`` sets, plus an
                # exhaustive ivfflat scan: the assertions below test the query,
                # not approximate-index recall over a handful of rows.
                await conn.execute(
                    text(
                        "SELECT set_config('app.tenant_id', :tid, true),"
                        "       set_config('app.user_id', 'SYSTEM', true),"
                        "       set_config('app.actor', 'USER', true),"
                        "       set_config('ivfflat.probes', '32768', true)"
                    ),
                    {"tid": tenant_id},
                )
                session = AsyncSession(
                    bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint",
                )
                try:
                    yield session
                finally:
                    await session.close()
            finally:
                await outer.rollback()
    finally:
        await engine.dispose()


def _svc(session: AsyncSession, tenant_id: str, **kw: Any) -> IngestService:
    kw.setdefault("user_id", USER_A)
    return IngestService(session, InMemoryBlobStore(), FakeEmbeddings(), tenant_id=tenant_id, **kw)


async def _ingest(svc: IngestService, name: str, body: str, **kw: Any) -> str:
    doc = await svc.create_document(
        title=name, filename=f"{name}.txt", mime="text/plain", data=body.encode(), **kw,
    )
    _, chunks = await svc.ingest_document(doc.id)
    assert chunks == 1
    return doc.id


async def test_search_ranks_by_cosine_distance_and_applies_the_gate(
    pg_session: AsyncSession, tenant_id: str,
) -> None:
    svc = _svc(pg_session, tenant_id)
    exact = await _ingest(svc, "exact", "banana-token")
    partial = await _ingest(svc, "partial", "banana-token apple-token")
    await _ingest(svc, "unrelated", "coconut-token")  # cosine distance ≈ 1

    query_vec, partial_vec = await FakeEmbeddings().embed(
        ["banana-token", "banana-token apple-token"],
    )
    partial_similarity = float(np.dot(query_vec, partial_vec))  # both unit length
    assert 1.0 - partial_similarity < 0.6

    hits = await svc.search("banana-token", k=5)
    assert [h.document_id for h in hits] == [exact, partial]
    assert hits[0].score == pytest.approx(1.0, abs=1e-4)
    assert hits[1].score == pytest.approx(partial_similarity, abs=1e-4)
    assert all(isinstance(h.score, float) for h in hits)

    assert [h.document_id for h in await svc.search("banana-token", k=1)] == [exact]
    assert await svc.search("durian-token", k=5) == []

    tight = _svc(pg_session, tenant_id, search_max_distance=1e-3)
    assert [h.document_id for h in await tight.search("banana-token", k=5)] == [exact]


async def test_memory_and_knowledge_scopes_on_pgvector(
    pg_session: AsyncSession, tenant_id: str,
) -> None:
    marker = "memorymarker-token"
    a = _svc(pg_session, tenant_id, user_id=USER_A)
    b = _svc(pg_session, tenant_id, user_id=USER_B)
    a_mem = await _ingest(a, "memory-a", marker, origin_session_id="ses_it_a")
    b_mem = await _ingest(b, "memory-b", marker, origin_session_id="ses_it_b")
    shared = await _ingest(a, "shared", marker)

    async def found(svc: IngestService, **kw: Any) -> dict[str, str]:
        return {h.document_id: h.scope for h in await svc.search(marker, k=10, **kw)}

    assert await found(a, scope="memory") == {a_mem: "memory"}
    assert await found(a, scope="knowledge") == {shared: "knowledge"}
    assert await found(a, scope="all") == {a_mem: "memory", shared: "knowledge"}
    assert await found(b, scope="all") == {b_mem: "memory", shared: "knowledge"}
    assert await found(a, scope="all", exclude_origin_session_id="ses_it_a") == {
        shared: "knowledge",
    }
