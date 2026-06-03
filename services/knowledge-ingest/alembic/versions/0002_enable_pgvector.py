"""Enable pgvector: CREATE EXTENSION, alter embedding column, add IVFFlat index.

Revision ID: 0002_enable_pgvector
Revises: 0001_init
Create Date: 2026-06-03

Pre-conditions
--------------
* pgvector 0.8.0 extension files must be present on the Postgres server
  (/usr/local/lib/postgresql/vector.so and the matching .control / .sql).
* chunks table must be empty (embedding column type change is done with
  USING cast; a non-empty JSON column would require manual data migration).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_enable_pgvector"
down_revision: str | None = "0001_init"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EMBEDDING_DIM = 1536


def upgrade() -> None:
    # 1. Install the extension (idempotent).
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # 2. Change the embedding column from JSON to vector(1536).
    #    chunks table is empty so no data-conversion is needed; we can do a
    #    plain ALTER with USING to cast the (absent) data.
    op.execute(
        f"ALTER TABLE chunks "
        f"ALTER COLUMN embedding TYPE vector({EMBEDDING_DIM}) "
        f"USING embedding::text::vector({EMBEDDING_DIM})"
    )

    # 3. Add IVFFlat cosine index for ANN search.
    #    lists=100 is a good default; tune for production data volume.
    op.execute(
        "CREATE INDEX IF NOT EXISTS chunks_embedding_cosine_idx "
        "ON chunks USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS chunks_embedding_cosine_idx")
    op.execute(
        "ALTER TABLE chunks "
        "ALTER COLUMN embedding TYPE json "
        "USING to_json(embedding::text)"
    )
    # Note: we deliberately do NOT drop the extension on downgrade because
    # other tables or databases may rely on it.
