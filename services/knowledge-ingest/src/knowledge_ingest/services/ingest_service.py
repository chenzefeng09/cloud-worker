"""End-to-end ingest pipeline + folders + search.

Pipeline
--------
1. ``create_document``  — persist row + raw blob; status = PENDING.
2. ``ingest_document``  — parse → chunk → embed → bulk-insert chunks.
                          Sets status to INGESTING during the run and
                          READY / FAILED on completion.
3. ``search``           — embed query → pgvector cosine search on
                          Postgres, fall back to pure-Python cosine on
                          SQLite (unit-test path).

Folders + sensitivity
---------------------
Documents live in a tenant-scoped folder tree and carry an ``L1..L5``
sensitivity.  Search / list apply two gates: *reachability* (tenant knowledge
vs. per-user memory) and *visibility* (document sensitivity ``<=`` caller
clearance).

The pipeline is **synchronous** in this skeleton — the FastAPI route awaits the
full chain.
"""

from __future__ import annotations

import logging
import math
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from knowledge_ingest.blob.base import BlobStore, make_key
from knowledge_ingest.domain.models import EMBEDDING_DIM, Chunk, Document, Folder
from knowledge_ingest.domain.sensitivity import (
    allowed_levels_for,
    is_valid_sensitivity,
    max_sensitivity,
    normalize_clearance,
)
from knowledge_ingest.embeddings.base import EmbeddingClient
from knowledge_ingest.errors import (
    DocumentNotFoundError,
    EmbeddingDimensionError,
    FolderCycleError,
    FolderNameConflictError,
    FolderNotEmptyError,
    FolderNotFoundError,
    IngestFailedError,
    InvalidSensitivityError,
    UnsupportedMimeError,
)
from knowledge_ingest.parsers.base import chunk_text, parse_document
from knowledge_ingest.schemas.document import SearchHit

logger = logging.getLogger(__name__)

# Sentinel for PATCH-style updates: distinguishes "field omitted" from an
# explicit ``None`` (e.g. move-to-root sets ``folder_id`` to ``None``).
_UNSET: Any = object()


class IngestService:
    def __init__(
        self,
        db: AsyncSession,
        blob: BlobStore,
        embeddings: EmbeddingClient,
        *,
        tenant_id: str,
        user_id: str = "SYSTEM",
        chunk_size: int = 800,
        chunk_overlap: int = 80,
        search_max_distance: float = 0.6,
    ) -> None:
        self._db = db
        self._blob = blob
        self._embeddings = embeddings
        self._tenant_id = tenant_id
        self._user_id = user_id
        self._chunk_size = chunk_size
        self._chunk_overlap = chunk_overlap
        self._search_max_distance = search_max_distance

    def _real_user_id(self) -> str | None:
        """The caller as a real user id, or None for the SYSTEM/service actor."""
        uid = self._user_id
        return uid if uid and uid != "SYSTEM" else None

    # ------------------------------------------------------------- folders

    async def list_folders(self) -> list[Folder]:
        stmt = (
            select(Folder)
            .where(Folder.tenant_id == self._tenant_id)
            .order_by(Folder.name.asc())
        )
        result = await self._db.execute(stmt)
        return list(result.scalars().all())

    async def get_folder(self, folder_id: str) -> Folder:
        stmt = select(Folder).where(
            Folder.id == folder_id, Folder.tenant_id == self._tenant_id,
        )
        folder = (await self._db.execute(stmt)).scalar_one_or_none()
        if folder is None:
            raise FolderNotFoundError(folder_id)
        return folder

    async def _name_taken(
        self, *, parent_id: str | None, name: str, exclude_id: str | None = None,
    ) -> bool:
        stmt = select(Folder.id).where(
            Folder.tenant_id == self._tenant_id,
            Folder.name == name,
        )
        stmt = stmt.where(
            Folder.parent_id.is_(None) if parent_id is None else Folder.parent_id == parent_id
        )
        if exclude_id is not None:
            stmt = stmt.where(Folder.id != exclude_id)
        return (await self._db.execute(stmt.limit(1))).first() is not None

    async def create_folder(
        self,
        *,
        name: str,
        parent_id: str | None = None,
        default_sensitivity: str = "L1",
    ) -> Folder:
        name = (name or "").strip()
        if not name:
            raise FolderNameConflictError(name)  # empty names are never valid
        if not is_valid_sensitivity(default_sensitivity):
            raise InvalidSensitivityError(default_sensitivity)
        if parent_id is not None:
            await self.get_folder(parent_id)  # 404 if parent missing / other tenant
        if await self._name_taken(parent_id=parent_id, name=name):
            raise FolderNameConflictError(name)

        folder = Folder(
            tenant_id=self._tenant_id,
            parent_id=parent_id,
            name=name,
            default_sensitivity=default_sensitivity,
            created_by_user_id=self._real_user_id(),
        )
        self._db.add(folder)
        await self._db.flush()
        await self._db.refresh(folder)
        return folder

    async def _descendant_ids(self, folder_id: str) -> set[str]:
        """Ids of ``folder_id`` and everything beneath it (cycle-safe)."""
        folders = await self.list_folders()
        children: dict[str | None, list[str]] = {}
        for f in folders:
            children.setdefault(f.parent_id, []).append(f.id)
        seen: set[str] = set()
        stack = [folder_id]
        while stack:
            fid = stack.pop()
            if fid in seen:
                continue
            seen.add(fid)
            stack.extend(children.get(fid, []))
        return seen

    async def update_folder(
        self,
        folder_id: str,
        *,
        name: Any = _UNSET,
        parent_id: Any = _UNSET,
        default_sensitivity: Any = _UNSET,
    ) -> Folder:
        folder = await self.get_folder(folder_id)

        new_parent = folder.parent_id
        new_name = folder.name

        if parent_id is not _UNSET:
            if parent_id is not None:
                if parent_id == folder_id:
                    raise FolderCycleError(folder_id)
                await self.get_folder(parent_id)  # 404 if missing / other tenant
                if parent_id in await self._descendant_ids(folder_id):
                    raise FolderCycleError(folder_id)
            new_parent = parent_id

        if name is not _UNSET:
            candidate = (name or "").strip()
            if not candidate:
                raise FolderNameConflictError(candidate)
            new_name = candidate

        # Re-check uniqueness whenever the (parent, name) pair changed.
        if (new_parent, new_name) != (folder.parent_id, folder.name) and await self._name_taken(
            parent_id=new_parent, name=new_name, exclude_id=folder_id,
        ):
            raise FolderNameConflictError(new_name)

        folder.parent_id = new_parent
        folder.name = new_name

        if default_sensitivity is not _UNSET and default_sensitivity is not None:
            if not is_valid_sensitivity(default_sensitivity):
                raise InvalidSensitivityError(default_sensitivity)
            folder.default_sensitivity = default_sensitivity

        await self._db.flush()
        await self._db.refresh(folder)
        return folder

    async def delete_folder(self, folder_id: str) -> None:
        folder = await self.get_folder(folder_id)

        has_child = (
            await self._db.execute(
                select(Folder.id)
                .where(Folder.tenant_id == self._tenant_id, Folder.parent_id == folder_id)
                .limit(1)
            )
        ).first() is not None
        has_doc = (
            await self._db.execute(
                select(Document.id)
                .where(Document.tenant_id == self._tenant_id, Document.folder_id == folder_id)
                .limit(1)
            )
        ).first() is not None
        if has_child or has_doc:
            raise FolderNotEmptyError(folder_id)

        await self._db.delete(folder)
        await self._db.flush()

    # ----------------------------------------------------------- documents

    async def list_documents(
        self,
        *,
        include_memory: bool = False,
        folder_id: Any = _UNSET,
        clearance: str | None = None,
    ) -> list[tuple[Document, int]]:
        """List the tenant's documents with a per-document chunk count.

        By default (``include_memory=False``) session-memory documents — rows
        stamped with a non-NULL ``origin_session_id`` by the
        session-orchestrator and titled ``"[memory] …"`` — are hidden, so the
        ordinary resource library shows only real uploads.

        ``include_memory=True`` drops that predicate entirely and returns
        *every* document in the tenant, including all users' memory rows (the
        admin-audit view).

        ``folder_id`` (when supplied) filters to one folder; pass ``None`` for
        the root layer.  ``clearance`` (the raw ``X-User-Clearance`` value)
        applies the visibility gate: when present, documents whose sensitivity
        exceeds it are dropped.  When absent (``None``) no visibility filtering
        is applied — this keeps the admin-audit / management listing complete
        (the BFF forwards the header for the end-user browsing path).
        """
        stmt = (
            select(Document, func.count(Chunk.id))
            .outerjoin(Chunk, Chunk.document_id == Document.id)
            .where(Document.tenant_id == self._tenant_id)
        )
        if not include_memory:
            stmt = stmt.where(Document.origin_session_id.is_(None))
        if folder_id is not _UNSET:
            stmt = stmt.where(
                Document.folder_id.is_(None) if folder_id is None
                else Document.folder_id == folder_id
            )
        if clearance is not None:
            stmt = stmt.where(Document.sensitivity.in_(allowed_levels_for(clearance)))
        stmt = stmt.group_by(Document.id).order_by(Document.uploaded_at.desc())
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
        self,
        *,
        title: str,
        filename: str,
        mime: str,
        data: bytes,
        origin_session_id: str | None = None,
        folder_id: str | None = None,
        sensitivity: str | None = None,
    ) -> Document:
        # Reject unsupported MIMEs up-front so we don't burn blob storage
        # on something the parser can't process later.
        if mime not in {"text/plain", "text/markdown", "application/pdf"}:
            raise UnsupportedMimeError(mime)

        folder: Folder | None = None
        if folder_id is not None:
            folder = await self.get_folder(folder_id)  # 404 if missing / other tenant

        # Resolve sensitivity: an explicit value wins; otherwise inherit the
        # folder default (root uploads default to L1).
        if sensitivity is not None:
            if not is_valid_sensitivity(sensitivity):
                raise InvalidSensitivityError(sensitivity)
            resolved_sensitivity = sensitivity
        else:
            resolved_sensitivity = folder.default_sensitivity if folder else "L1"

        # Memory docs (stamped with an ``origin_session_id`` by the
        # session-orchestrator) are owned by the requesting user so search can
        # scope them per-user. Ordinary uploads keep ``origin_user_id`` NULL and
        # stay tenant-shared.
        origin_user_id = self._user_id if origin_session_id else None
        doc = Document(
            tenant_id=self._tenant_id,
            title=title,
            mime=mime,
            status="PENDING",
            origin_session_id=origin_session_id,
            origin_user_id=origin_user_id,
            folder_id=folder_id,
            sensitivity=resolved_sensitivity,
            uploaded_by_user_id=self._real_user_id(),
        )
        self._db.add(doc)
        await self._db.flush()
        await self._db.refresh(doc)

        key = make_key(self._tenant_id, doc.id, filename)
        blob_url = self._blob.put(key, data, content_type=mime)
        doc.source_uri = blob_url
        await self._db.flush()
        return doc

    async def update_document(
        self,
        document_id: str,
        *,
        folder_id: Any = _UNSET,
        sensitivity: Any = _UNSET,
    ) -> tuple[Document, int]:
        """Move a document and/or change its sensitivity.

        * Moving into a folder without an explicit sensitivity elevates only:
          ``sensitivity = max(current, folder.default_sensitivity)``.
        * An explicit ``sensitivity`` is applied verbatim (the sole way to
          *lower* a level) and takes precedence over the move elevation.
        """
        doc, _ = await self.get_document(document_id)

        target_folder: Any = _UNSET
        if folder_id is not _UNSET:
            if folder_id is not None:
                target_folder = await self.get_folder(folder_id)  # 404 if missing
            else:
                target_folder = None
            doc.folder_id = folder_id

        if sensitivity is not _UNSET and sensitivity is not None:
            if not is_valid_sensitivity(sensitivity):
                raise InvalidSensitivityError(sensitivity)
            doc.sensitivity = sensitivity
        elif target_folder is not _UNSET and target_folder is not None:
            doc.sensitivity = max_sensitivity(doc.sensitivity, target_folder.default_sensitivity)

        await self._db.flush()
        return await self.get_document(document_id)

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

    def _scope_clause(self, scope: str, allowed_levels: list[str]):  # noqa: ANN202
        """Reachability + visibility predicate for a search scope.

        * ``knowledge`` — tenant-shared uploads (``origin_session_id`` IS NULL),
          gated by sensitivity <= clearance.
        * ``memory``    — the caller's own session-memory docs
          (``origin_user_id`` == caller); not sensitivity-gated (owner-private).
          A memory doc with NULL ``origin_user_id`` (un-backfilled legacy row)
          is excluded — fail-closed.
        * ``all``       — union of the two (default; backward-compatible).
        """
        knowledge = (Document.origin_session_id.is_(None)) & (
            Document.sensitivity.in_(allowed_levels)
        )
        memory = Document.origin_user_id == self._user_id
        if scope == "knowledge":
            return knowledge
        if scope == "memory":
            return memory
        return knowledge | memory

    async def _folder_path_map(self) -> dict[str, str]:
        """Map every tenant folder id → its full slash-joined path.

        Root-level folders map to their own name; nested folders to
        ``parent/child`` (e.g. ``产品/合同模板``).  Cycle-safe.
        """
        folders = await self.list_folders()
        by_id = {f.id: f for f in folders}
        cache: dict[str, str] = {}

        def path_for(fid: str, seen: set[str]) -> str:
            if fid in cache:
                return cache[fid]
            if fid in seen:
                return ""  # defensive: should not happen (cycles are rejected)
            f = by_id.get(fid)
            if f is None:
                return ""
            seen.add(fid)
            parent_path = path_for(f.parent_id, seen) if f.parent_id else ""
            resolved = f"{parent_path}/{f.name}" if parent_path else f.name
            cache[fid] = resolved
            return resolved

        for fid in by_id:
            path_for(fid, set())
        return cache

    async def search(
        self,
        query: str,
        *,
        k: int = 5,
        scope: str = "all",
        clearance: str = "L1",
        exclude_origin_session_id: str | None = None,
        workspace_id: str | None = None,  # noqa: ARG002 — reserved; effective clearance is
        # computed upstream (session-orchestrator) and passed via ``clearance``.
    ) -> list[SearchHit]:
        """Vector search over chunks with reachability + visibility gates.

        ``scope`` selects ``knowledge`` (tenant uploads), ``memory`` (the
        caller's own session memory) or ``all`` (both — the default, kept for
        backward compatibility).  ``clearance`` drops knowledge hits above the
        caller's level.  Hits whose cosine distance exceeds
        ``search_max_distance`` are discarded regardless of scope.

        ``exclude_origin_session_id`` (when non-empty) drops every chunk whose
        parent document was produced by that session — used by the
        session-orchestrator memory pre-fetch to avoid re-injecting a session's
        own prior-turn Q&A that is already restored via conversation history.
        """
        if not query.strip():
            return []
        vectors = await self._embeddings.embed([query])
        if not vectors:
            return []
        qvec = vectors[0]
        if len(qvec) != EMBEDDING_DIM:
            raise EmbeddingDimensionError(EMBEDDING_DIM, len(qvec))

        exclude = (exclude_origin_session_id or "").strip() or None
        allowed_levels = allowed_levels_for(normalize_clearance(clearance))
        scope_clause = self._scope_clause(scope, allowed_levels)

        bind = self._db.get_bind()
        if bind is not None and bind.dialect.name == "postgresql":
            rows = await self._search_pgvector(
                qvec, k=k, scope_clause=scope_clause, exclude_origin_session_id=exclude,
            )
        else:
            rows = await self._search_python(
                qvec, k=k, scope_clause=scope_clause, exclude_origin_session_id=exclude,
            )

        folder_paths = await self._folder_path_map()
        hits: list[SearchHit] = []
        for chunk, doc, score in rows:
            hits.append(
                SearchHit(
                    document_id=doc.id,
                    document_name=doc.title,
                    folder_path=folder_paths.get(doc.folder_id, "") if doc.folder_id else "",
                    chunk_id=chunk.id,
                    ord=chunk.ord,
                    text=chunk.text,
                    score=float(score),
                    scope="memory" if doc.origin_session_id else "knowledge",
                )
            )
        return hits

    async def _search_pgvector(
        self,
        qvec: list[float],
        *,
        k: int,
        scope_clause: Any,
        exclude_origin_session_id: str | None = None,
    ) -> list[tuple[Chunk, Document, float]]:
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
            .where(scope_clause)
            .where(distance <= self._search_max_distance)
            .order_by(distance.asc())
            .limit(k)
        )
        if exclude_origin_session_id is not None:
            stmt = stmt.where(
                (Document.origin_session_id.is_(None))
                | (Document.origin_session_id != exclude_origin_session_id)
            )
        result = await self._db.execute(stmt)
        return [(chunk, doc, float(1.0 - dist)) for chunk, doc, dist in result.all()]

    async def _search_python(
        self,
        qvec: list[float],
        *,
        k: int,
        scope_clause: Any,
        exclude_origin_session_id: str | None = None,
    ) -> list[tuple[Chunk, Document, float]]:
        """Fallback for unit tests on SQLite — small dataset, fine to brute force."""
        stmt = (
            select(Chunk, Document)
            .join(Document, Document.id == Chunk.document_id)
            .where(Document.tenant_id == self._tenant_id)
            .where(scope_clause)
        )
        if exclude_origin_session_id is not None:
            stmt = stmt.where(
                (Document.origin_session_id.is_(None))
                | (Document.origin_session_id != exclude_origin_session_id)
            )
        result = await self._db.execute(stmt)
        rows = list(result.all())
        scored: list[tuple[float, Chunk, Document]] = []
        for chunk, doc in rows:
            vec = chunk.embedding or []
            sim = _cosine(qvec, vec)
            if (1.0 - sim) > self._search_max_distance:
                continue  # too far — drop it (same gate as the pgvector path)
            scored.append((sim, chunk, doc))
        scored.sort(key=lambda t: t[0], reverse=True)
        return [(chunk, doc, sim) for sim, chunk, doc in scored[:k]]


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)
