"""Dialect-aware vector column.

PostgreSQL → a TypeDecorator that serialises Python lists to vector text
literals ``[x,y,z,…]`` and emits ``CAST(:param AS vector)`` so asyncpg
can send a plain string while Postgres casts it to the ``vector`` type.
No numpy or pgvector Python package is needed.

SQLite (unit tests) → JSON-encoded list of floats.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import String
from sqlalchemy.types import TypeDecorator, UserDefinedType


class _VectorType(UserDefinedType):  # type: ignore[type-arg]
    """Minimal SQLAlchemy type that renders as ``vector`` in DDL / CAST."""

    cache_ok = True

    def get_col_spec(self, **kw: Any) -> str:  # noqa: ANN401
        return "vector"


class VectorColumn(TypeDecorator):  # type: ignore[type-arg]
    """Store vectors as text on Postgres (CAST AS vector), JSON on SQLite."""

    impl = String
    cache_ok = True

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


__all__ = ["VectorColumn"]
