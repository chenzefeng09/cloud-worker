"""FastAPI dependencies — tenant context, DB session, blob, embeddings."""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import Depends, Header, HTTPException, Request, status
from foundation_sdk.fastapi import get_tenant_id, get_user_id
from sqlalchemy.ext.asyncio import AsyncSession

from knowledge_ingest.blob.base import BlobStore
from knowledge_ingest.domain.db import get_session
from knowledge_ingest.embeddings.base import EmbeddingClient


async def current_tenant() -> str:
    tid = get_tenant_id()
    if not tid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="X-Tenant-Id header required",
        )
    return tid


async def current_user() -> str:
    return get_user_id() or "SYSTEM"


async def current_clearance(
    x_user_clearance: str | None = Header(default=None),
) -> str | None:
    """Raw ``X-User-Clearance`` header (L1..L5) or ``None`` when absent.

    Endpoints decide the fallback: ``/search`` normalises a missing value to
    the conservative ``L1`` (fail-closed retrieval), while ``/documents``
    treats ``None`` as "no visibility filter" so the management/audit listing
    stays complete.  The BFF forwards this header on the end-user browsing path.
    """
    return x_user_clearance


async def db_session(
    tenant_id: str = Depends(current_tenant),
    user_id: str = Depends(current_user),
) -> AsyncIterator[AsyncSession]:
    async for session in get_session(tenant_id=tenant_id, user_id=user_id):
        yield session


def get_blob_store(request: Request) -> BlobStore:
    blob_store: BlobStore = request.app.state.blob_store
    return blob_store


def get_embeddings(request: Request) -> EmbeddingClient:
    embeddings: EmbeddingClient = request.app.state.embeddings
    return embeddings


TDbSession = Depends(db_session)
TTenantId = Depends(current_tenant)
TUserId = Depends(current_user)
TClearance = Depends(current_clearance)
TBlobStore = Depends(get_blob_store)
TEmbeddings = Depends(get_embeddings)
