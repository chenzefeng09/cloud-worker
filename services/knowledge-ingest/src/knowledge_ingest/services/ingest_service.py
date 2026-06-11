"""End-to-end ingest pipeline + search.

Pipeline
--------
1. ``create_document``  — persist row + raw blob; status = PENDING.
2. ``ingest_document``  — parse → chunk → embed → bulk-insert chunks.
                          Sets status to INGESTING during the run and
                          READY / FAILED on completion.
3. ``search``           — embed query → pgvector cosine search on
                          Postgres, fall back to pure-Python cosine on
                          SQLite (unit-test path).

The pipeline is **synchronous** in this skeleton — the FastAPI route
awaits the full chain.  A future worker can pick up PENDING documents
asynchronously by reusing ``IngestService.ingest_document``.
"""

from __future__ import annotations

import logging
import math

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from knowledge_ingest.blob.base import BlobStore, make_key
from knowledge_ingest.domain.models import EMBEDDING_DIM, Chunk, Document
from knowledge_ingest.embeddings.base import EmbeddingClient
from knowledge_ingest.errors import (
    DocumentNotFoundError,
    EmbeddingDimensionError,
    IngestFailedError,
    UnsupportedMimeError,
)
from knowledge_ingest.parsers.base import chunk_text, parse_document
from knowledge_ingest.schemas.document import SearchHit

logger = logging.getLogger(__name__)


class IngestService:
    def __init__(
        self,
        db: AsyncSession,
        blob: BlobStore,
        embeddings: EmbeddingClient,
        *,
        tenant_id: str,
        chunk_size: int = 800,
        chunk_overlap: int = 80,
    ) -> None:
        self._db = db
        self._blob = blob
        self._embeddings = embeddings
        self._tenant_id = tenant_id
        self._chunk_size = chunk_size
        self._chunk_overlap = chunk_overlap

    # ----------------------------------------------------------- documents

    async def list_documents(self) -> list[tuple[Document, int]]:
        stmt = (
            select(Document, func.count(Chunk.id))
            .outerjoin(Chunk, Chunk.document_id == Document.id)
            .where(Document.tenant_id == self._tenant_id)
            .group_by(Document.id)
            .order_by(Document.uploaded_at.desc())
        )
        result = await self._db.execute(stmt)
        return [(doc, count) for doc, count in result.all()]  # noqa: C416

    async def get_document(self, document_id: str) -> tuple[Document, int]:
        stmt = (
            select(Document, func.count(Chunk.id))
            .outerjoin(Chunk, Chunk.document_id == Document.id)
            .where(
                Document.id == document_id, Document.tenant_id == self._tenant_id,
            )
            .group_by(Document.id)
        )
        result = await self._db.execute(stmt)
        row = result.first()
        if row is None:
            raise DocumentNotFoundError(document_id)
        doc, count = row
        return doc, count

    async def create_document(
        self, *, title: str, filename: str, mime: str, data: bytes,
    ) -> Document:
        # Reject unsupported MIMEs up-front so we don't burn blob storage
        # on something the parser can't process later.
        if mime not in {"text/plain", "text/markdown", "application/pdf"}:
            raise UnsupportedMimeError(mime)

        doc = Document(
            tenant_id=self._tenant_id,
            title=title,
            mime=mime,
            status="PENDING",
        )
        self._db.add(doc)
        await self._db.flush()
        await self._db.refresh(doc)

        key = make_key(self._tenant_id, doc.id, filename)
        blob_url = self._blob.put(key, data, content_type=mime)
        doc.source_uri = blob_url
        await self._db.flush()
        return doc

    async def delete_document(self, document_id: str) -> None:
        """Delete a document and its chunks (cascade) + best-effort source blob.

        The DB row is dropped via the ORM (chunks cascade on the FK).  The
        source blob is NOT covered by the DB cascade, so we delete it
        best-effort first: a blob delete failure is logged and skipped rather
        than aborting the whole operation — object storage cleanup is
        idempotent and any leaked object can be GC'd later.
        """
        doc, _ = await self.get_document(document_id)

        if doc.source_uri is not None:
            key = doc.source_uri.replace("memory://", "")
            if doc.source_uri.startswith("s3://"):
                key = doc.source_uri.split("/", 3)[-1]
            try:
                self._blob.delete(key)
            except Exception:  # noqa: BLE001 — tolerate blob leak, keep going
                logger.warning(
                    "blob delete failed during document delete; leaking object",
                    extra={"document_id": doc.id, "key": key},
                    exc_info=True,
                )

        await self._db.delete(doc)
        await self._db.flush()

    async def ingest_document(self, document_id: str) -> tuple[Document, int]:
        doc, _ = await self.get_document(document_id)
        if doc.source_uri is None:
            raise IngestFailedError(document_id, "document has no source blob")

        key = doc.source_uri.replace("memory://", "")
        if doc.source_uri.startswith("s3://"):
            key = doc.source_uri.split("/", 3)[-1]
        raw = self._blob.get(key)

        doc.status = "INGESTING"
        await self._db.flush()

        try:
            text = parse_document(mime=doc.mime, data=raw)
            pieces = chunk_text(text, chunk_size=self._chunk_size, overlap=self._chunk_overlap)
            vectors = await self._embeddings.embed(pieces) if pieces else []

            for v in vectors:
                if len(v) != EMBEDDING_DIM:
                    raise EmbeddingDimensionError(EMBEDDING_DIM, len(v))

            for ord_, (piece, vec) in enumerate(zip(pieces, vectors, strict=True)):
                self._db.add(
                    Chunk(
                        document_id=doc.id,
                        ord=ord_,
                        text=piece,
                        embedding=vec,
                    )
                )
            doc.status = "READY"
            await self._db.flush()
            return doc, len(pieces)
        except Exception as exc:
            doc.status = "FAILED"
            await self._db.flush()
            if isinstance(exc, IngestFailedError | EmbeddingDimensionError | UnsupportedMimeError):
                raise
            raise IngestFailedError(doc.id, str(exc)) from exc

    # ----------------------------------------------------------- search

    async def search(self, query: str, *, k: int = 5) -> list[SearchHit]:
        if not query.strip():
            return []
        vectors = await self._embeddings.embed([query])
        if not vectors:
            return []
        qvec = vectors[0]
        if len(qvec) != EMBEDDING_DIM:
            raise EmbeddingDimensionError(EMBEDDING_DIM, len(qvec))

        bind = self._db.get_bind()
        if bind is not None and bind.dialect.name == "postgresql":
            return await self._search_pgvector(qvec, k=k)
        return await self._search_python(qvec, k=k)

    async def _search_pgvector(self, qvec: list[float], *, k: int) -> list[SearchHit]:
        # pgvector ships ``<=>`` (cosine distance) as an operator on the
        # ``vector`` type.  Lower distance = better match.
        #
        # We embed the query vector as an inline text literal cast to vector,
        # which lets asyncpg bind it without needing pgvector's numpy-backed codec.
        from sqlalchemy import text as sa_text

        vec_text = "[" + ",".join(str(float(v)) for v in qvec) + "]"
        # Build: "chunks.embedding <=> CAST('[x,y,z]' AS vector)" as a column expression.
        distance = Chunk.embedding.op("<=>")(
            sa_text(f"CAST('{vec_text}' AS vector)")
        )
        stmt = (
            select(Chunk, Document, distance.label("dist"))
            .join(Document, Document.id == Chunk.document_id)
            .where(Document.tenant_id == self._tenant_id)
            .order_by(distance.asc())
            .limit(k)
        )
        result = await self._db.execute(stmt)
        hits: list[SearchHit] = []
        for chunk, doc, dist in result.all():
            hits.append(
                SearchHit(
                    document_id=doc.id,
                    chunk_id=chunk.id,
                    ord=chunk.ord,
                    text=chunk.text,
                    score=float(1.0 - dist),
                )
            )
        return hits

    async def _search_python(self, qvec: list[float], *, k: int) -> list[SearchHit]:
        """Fallback for unit tests on SQLite — small dataset, fine to brute force."""
        stmt = (
            select(Chunk, Document)
            .join(Document, Document.id == Chunk.document_id)
            .where(Document.tenant_id == self._tenant_id)
        )
        result = await self._db.execute(stmt)
        rows = list(result.all())
        scored: list[tuple[float, Chunk, Document]] = []
        for chunk, doc in rows:
            vec = chunk.embedding or []
            scored.append((_cosine(qvec, vec), chunk, doc))
        scored.sort(key=lambda t: t[0], reverse=True)
        hits: list[SearchHit] = []
        for score, chunk, doc in scored[:k]:
            hits.append(
                SearchHit(
                    document_id=doc.id,
                    chunk_id=chunk.id,
                    ord=chunk.ord,
                    text=chunk.text,
                    score=score,
                )
            )
        return hits


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)
