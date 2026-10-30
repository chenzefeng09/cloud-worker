"""PostgreSQL-dialect tests for the pgvector search SQL.

The other search tests run on SQLite, where ``IngestService.search`` takes
the pure-Python path, so the pgvector statement is never compiled for
PostgreSQL.  These tests compile it for ``postgresql+asyncpg``.  They run
every bind processor the way execution does before asyncpg sees the
parameters, and read the distance column back through its result processor.
That is where the search failed: ``Chunk.embedding.op("<=>")`` typed the
distance as a vector, so ``process_bind_param`` received the float
distance threshold.
"""

from __future__ import annotations

import re
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from sqlalchemy import Float, Integer, insert, select
from sqlalchemy.dialects.postgresql import asyncpg as pg_asyncpg
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.compiler import SQLCompiler
from sqlalchemy.sql.elements import ClauseElement, ColumnElement
from sqlalchemy.types import TypeEngine

from knowledge_ingest.blob.memory import InMemoryBlobStore
from knowledge_ingest.domain.models import EMBEDDING_DIM, Chunk, Document
from knowledge_ingest.domain.types import VectorColumn
from knowledge_ingest.services.ingest_service import IngestService
from tests.conftest import TEST_TENANT, TEST_USER, FakeEmbeddings

PG = pg_asyncpg.dialect()
# asyncpg reports pgvector distances (``double precision``) with this type OID.
FLOAT8_OID = 701

QVEC = [0.5, -0.25] + [0.0] * (EMBEDDING_DIM - 2)


def _vector_text(vec: list[float]) -> str:
    return "[" + ",".join(str(float(v)) for v in vec) + "]"


def _compile(stmt: ClauseElement) -> SQLCompiler:
    return cast(SQLCompiler, stmt.compile(dialect=PG))


def _sql(compiled: SQLCompiler) -> str:
    return " ".join(str(compiled).split())


def _bound(compiled: SQLCompiler) -> list[tuple[TypeEngine[Any], Any]]:
    """Every parameter as ``(type, value sent to asyncpg)``.

    Runs each type's bind processor the way ``DefaultExecutionContext`` does
    before execution (per element for an expanding ``IN`` parameter).  A
    ``process_bind_param`` that cannot handle its value raises here.
    """
    assert compiled.positiontup is not None  # asyncpg is a positional paramstyle
    params = compiled.construct_params()
    out: list[tuple[TypeEngine[Any], Any]] = []
    for name in compiled.positiontup:
        bind = compiled.binds[name]
        processor = bind.type.dialect_impl(PG).bind_processor(PG)
        value = params[name]
        if processor is not None:
            value = [processor(v) for v in value] if bind.expanding else processor(value)
        out.append((bind.type, value))
    return out


def _values_of(bound: list[tuple[TypeEngine[Any], Any]], type_: type[Any]) -> list[Any]:
    return [value for t, value in bound if isinstance(t, type_)]


def _read_float8(column: ColumnElement[Any], raw: float) -> Any:
    """``raw`` as a float8 column value after ``column``'s result processor."""
    processor = column.type.dialect_impl(PG).result_processor(PG, FLOAT8_OID)
    return processor(raw) if processor is not None else raw


# ------------------------------------------------------------ the column type


@pytest.mark.parametrize(
    ("method", "opstring"),
    [
        ("cosine_distance", "<=>"),
        ("l2_distance", "<->"),
        ("max_inner_product", "<#>"),
        ("l1_distance", "<+>"),
    ],
)
def test_distance_operators_are_float_typed_on_postgresql(method: str, opstring: str) -> None:
    distance = getattr(Chunk.embedding, method)(QVEC)
    assert isinstance(distance.type, Float)

    stmt = (
        select(Chunk.id, distance.label("d"))
        .where(distance <= 0.5)
        .order_by(distance)
    )
    compiled = _compile(stmt)
    sql = _sql(compiled)

    # The query vector is one bound parameter, cast to vector by Postgres.
    assert sql.count(opstring) == 3
    assert sql.count(f"chunks.embedding {opstring} CAST($1::VARCHAR AS vector)") == 3
    # The threshold is a float, not a vector.
    assert re.search(r"\) <= \$2::FLOAT", sql), sql

    bound = _bound(compiled)
    assert bound == [(bound[0][0], _vector_text(QVEC)), (bound[1][0], 0.5)]
    assert isinstance(bound[0][0], VectorColumn)
    assert isinstance(bound[1][0], Float)

    assert _read_float8(stmt.selected_columns["d"], 0.25) == 0.25


def test_vector_column_binds_and_reads_pgvector_text() -> None:
    compiled = _compile(insert(Chunk).values(
        id="chk_1", document_id="doc_1", ord=0, text="t", embedding=[0.5, -0.25, 1],
    ))
    assert "CAST($5::VARCHAR AS vector)" in _sql(compiled)
    assert _values_of(_bound(compiled), VectorColumn) == ["[0.5,-0.25,1.0]"]

    column_type = Chunk.__table__.c.embedding.type
    read = column_type.dialect_impl(PG).result_processor(PG, None)
    assert read("[0.5,-0.25,1]") == [0.5, -0.25, 1.0]


# ------------------------------------------------------- the search statement


def _service(**kw: Any) -> IngestService:
    return IngestService(
        MagicMock(), InMemoryBlobStore(), FakeEmbeddings(),
        tenant_id=TEST_TENANT, user_id=TEST_USER, **kw,
    )


def test_pgvector_search_stmt_binds_query_vector_and_float_threshold() -> None:
    svc = _service(search_max_distance=0.42)
    stmt = svc._pgvector_search_stmt(
        QVEC,
        k=7,
        scope_clause=svc._scope_clause("all", ["L1", "L2"]),
        exclude_origin_session_id="ses_self",
    )
    compiled = _compile(stmt)
    sql = _sql(compiled)

    # Bound, not interpolated: no vector literal in the SQL text, and the
    # select list, WHERE and ORDER BY all reuse parameter $1.
    assert "'[" not in sql
    assert sql.count("<=>") == 3
    assert sql.count("chunks.embedding <=> CAST($1::VARCHAR AS vector)") == 3
    assert re.search(r"CAST\(\$1::VARCHAR AS vector\)\) <= \$\d+::FLOAT", sql), sql
    assert re.search(
        r"ORDER BY \(chunks\.embedding <=> CAST\(\$1::VARCHAR AS vector\)\) ASC"
        r" LIMIT \$\d+::INTEGER",
        sql,
    ), sql

    bound = _bound(compiled)
    assert _values_of(bound, VectorColumn) == [_vector_text(QVEC)]
    assert _values_of(bound, Float) == [0.42]
    assert _values_of(bound, Integer) == [7]

    dist = stmt.selected_columns["dist"]
    assert isinstance(dist.type, Float)
    assert _read_float8(dist, 0.25) == 0.25


# ------------------------------------------------- search() on the pg branch


class _Result:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    def all(self) -> list[tuple[Any, ...]]:
        return list(self._rows)

    def scalars(self) -> _Result:
        return _Result([row[0] for row in self._rows])


class _PostgresSession:
    """Stands in for an ``AsyncSession`` bound to ``postgresql+asyncpg``.

    ``execute`` compiles each statement for PostgreSQL and runs its bind
    processors, as SQLAlchemy does before asyncpg sees the parameters.  The
    pgvector query gets ``rows`` back, with each raw ``float8`` distance run
    through the ``dist`` column's result processor; other queries (the
    folder listing) get no rows.
    """

    def __init__(self, rows: list[tuple[Chunk, Document, float]]) -> None:
        self._rows = rows
        self.compiled: list[str] = []

    def get_bind(self) -> Any:
        return SimpleNamespace(dialect=PG)

    async def execute(self, stmt: Any) -> _Result:
        compiled = _compile(stmt)
        _bound(compiled)
        self.compiled.append(_sql(compiled))
        if "dist" not in stmt.selected_columns:
            return _Result([])
        dist = stmt.selected_columns["dist"]
        return _Result([(c, d, _read_float8(dist, raw)) for c, d, raw in self._rows])


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["all", "knowledge", "memory"])
async def test_search_runs_the_pgvector_query_on_postgresql(scope: str) -> None:
    doc = Document(
        id="doc_PG", tenant_id=TEST_TENANT, title="Handbook", mime="text/plain",
        origin_session_id="ses_mem" if scope == "memory" else None,
        origin_user_id=TEST_USER if scope == "memory" else None,
    )
    chunk = Chunk(id="chk_PG", document_id=doc.id, ord=3, text="the text", embedding=QVEC)
    session = _PostgresSession([(chunk, doc, 0.25)])
    svc = IngestService(
        cast(AsyncSession, session),
        InMemoryBlobStore(), FakeEmbeddings(), tenant_id=TEST_TENANT, user_id=TEST_USER,
    )

    hits = await svc.search("handbook", k=3, scope=scope, clearance="L2")

    assert [(h.document_id, h.chunk_id, h.ord, h.text) for h in hits] == [
        ("doc_PG", "chk_PG", 3, "the text"),
    ]
    assert hits[0].score == pytest.approx(0.75)
    assert hits[0].scope == ("memory" if scope == "memory" else "knowledge")
    assert any("<=>" in sql for sql in session.compiled)
