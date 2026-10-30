"""Dialect-aware vector column.

PostgreSQL → a TypeDecorator that serialises Python lists to vector text
literals ``[x,y,z,…]`` and emits ``CAST(:param AS vector)`` so asyncpg
can send a plain string while Postgres casts it to the ``vector`` type.
No numpy or pgvector Python package is needed.

SQLite (unit tests) → JSON-encoded list of floats.

Distance operators
------------------
pgvector's distance operators are methods on the column, with the same names
as ``pgvector.sqlalchemy.Vector``::

    Chunk.embedding.cosine_distance(query_vec)    # <=>
    Chunk.embedding.l2_distance(query_vec)        # <->
    Chunk.embedding.max_inner_product(query_vec)  # <#>
    Chunk.embedding.l1_distance(query_vec)        # <+>

Each one returns a ``Float`` expression and binds ``query_vec`` as a parameter
of this type, which renders as ``CAST($n AS vector)``.  Do not build these
with ``Chunk.embedding.op("<=>")(…)``.  A custom ``op()`` without a
``return_type`` gets the type of its left operand, here this vector type.
Comparing that expression with a float threshold then sends the float
through ``process_bind_param`` (``TypeError: 'float' object is not
iterable``), and the threshold renders as ``CAST(… AS vector)``.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import Float, String
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql.elements import ColumnElement
from sqlalchemy.types import TypeDecorator, UserDefinedType


class _VectorType(UserDefinedType):  # type: ignore[type-arg]
    """Minimal SQLAlchemy type that renders as ``vector`` in DDL / CAST."""

    cache_ok = True

    def get_col_spec(self, **kw: Any) -> str:  # noqa: ANN401
        return "vector"


@compiles(_VectorType, "sqlite")
def _compile_vector_sqlite(type_: Any, compiler: Any, **kw: Any) -> str:  # noqa: ANN401
    """Render the CAST target as ``TEXT`` on SQLite (unit-test path).

    SQLite has no ``vector`` type and assigns unknown type names NUMERIC
    affinity, so ``CAST('[0.1,…]' AS vector)`` would silently coerce the JSON
    vector literal to ``0``.  Rendering ``TEXT`` keeps the JSON string intact
    for ``process_result_value`` to parse back into a list.  Postgres keeps the
    real ``vector`` type via ``get_col_spec``.
    """
    return "TEXT"


class VectorComparator(TypeDecorator.Comparator[Any]):
    """pgvector distance operators for ``VectorColumn``, each typed ``Float``.

    pgvector's distance operators return ``double precision``.  Typing the
    expression as ``Float`` makes a comparison such as ``distance <= 0.6``
    bind the threshold as a float, and makes result rows use the ``Float``
    result processor instead of the vector one.  ``other`` is a vector, either
    a Python sequence (bound as a ``VectorColumn`` parameter) or a SQL
    expression.  These operators are PostgreSQL-only; SQLite has no pgvector.
    """

    __slots__ = ()

    def _distance(self, opstring: str, other: Any) -> ColumnElement[float]:
        return self.expr.op(opstring, return_type=Float)(other)

    def l2_distance(self, other: Any) -> ColumnElement[float]:
        """``<->``: Euclidean (L2) distance."""
        return self._distance("<->", other)

    def max_inner_product(self, other: Any) -> ColumnElement[float]:
        """``<#>``: the *negative* inner product (lower = more similar)."""
        return self._distance("<#>", other)

    def cosine_distance(self, other: Any) -> ColumnElement[float]:
        """``<=>``: cosine distance in ``[0, 2]`` (0 = same direction)."""
        return self._distance("<=>", other)

    def l1_distance(self, other: Any) -> ColumnElement[float]:
        """``<+>``: taxicab (L1) distance (pgvector >= 0.7)."""
        return self._distance("<+>", other)


class VectorColumn(TypeDecorator):  # type: ignore[type-arg]
    """Store vectors as text on Postgres (CAST AS vector), JSON on SQLite."""

    impl = String
    cache_ok = True
    # Replaces TypeDecorator's default comparator (built from ``impl``, i.e.
    # String's).  String operators such as ``+`` → ``||`` mean nothing for a
    # vector.  This one adds the pgvector distance methods above.
    comparator_factory = VectorComparator

    def __init__(self, dim: int) -> None:
        super().__init__()
        self.dim = dim

    # ------------------------------------------------------------------
    # Dialect impl selection
    # ------------------------------------------------------------------

    def load_dialect_impl(self, dialect: Any) -> Any:
        return dialect.type_descriptor(String())

    # ------------------------------------------------------------------
    # Write path: Python list → DB
    # ------------------------------------------------------------------

    def process_bind_param(self, value: Any, dialect: Any) -> Any:
        if value is None:
            return None
        if dialect.name == "postgresql":
            # Encode as pgvector text literal "[x,y,z,…]"
            return "[" + ",".join(str(float(v)) for v in value) + "]"
        return json.dumps(list(value))

    def bind_expression(self, bindvalue: Any) -> Any:
        """Wrap bound param in CAST(… AS vector) for Postgres, so asyncpg
        sends a text string that Postgres can implicitly upcast to vector.

        On SQLite this is a no-op — SQLAlchemy detects the dialect and
        the rendered SQL never includes the cast.
        """
        from sqlalchemy import cast
        return cast(bindvalue, _VectorType())

    # ------------------------------------------------------------------
    # Read path: DB → Python list
    # ------------------------------------------------------------------

    def process_result_value(self, value: Any, dialect: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, list | tuple):
            return [float(v) for v in value]
        if isinstance(value, str):
            s = value.strip()
            if s.startswith("["):
                inner = s[1:-1]
                return [float(v) for v in inner.split(",") if v]
            return json.loads(s)
        return value


__all__ = ["VectorColumn", "VectorComparator"]
