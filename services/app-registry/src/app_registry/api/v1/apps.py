"""App + AppVersion endpoints — CRUD, versioning, review, internal resolve.

Every public route is guarded fail-closed by ``require_permission(action,
"app")``.  Owner-only mutations additionally enforce ``author_id == user_id``
in the service layer; ``app.review`` holders (admins) bypass that check via the
``is_reviewer`` dependency.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app_registry.api.authz_guard import is_reviewer, require_permission
from app_registry.api.dependencies import (
    TDbSession,
    TInternalAuth,
    TTenantId,
    TUserId,
    internal_db_session,
)
from app_registry.schemas.app import (
    AppCreate,
    AppOut,
    AppReviewItem,
    AppUpdate,
    AppVersionCreate,
    AppVersionOut,
    ReviewDecision,
)
from app_registry.schemas.common import ListPage
from app_registry.services.app_service import AppService

router = APIRouter(tags=["Apps"])

TIsReviewer = Depends(is_reviewer())


# ---------------------------------------------------------------------------
# Review queue (registered before /apps/{app_id} for clarity; path depth makes
# them unambiguous either way).
# ---------------------------------------------------------------------------
@router.get(
    "/apps/review/pending",
    response_model=ListPage,
    dependencies=[Depends(require_permission("app.review", "app"))],
)
async def review_pending(
    db: AsyncSession = TDbSession,
    tenant_id: str = TTenantId,
    user_id: str = TUserId,
) -> ListPage:
    """Admin review queue — pending_review versions with App name + author."""
    svc = AppService(db, tenant_id=tenant_id, user_id=user_id)
    pairs = await svc.list_pending()
    items = [
        AppReviewItem(
            version=AppVersionOut.model_validate(ver),
            app_id=app.id,
            app_name=app.name,
            author_id=app.author_id,
        ).model_dump(mode="json")
        for ver, app in pairs
    ]
    return ListPage(items=items, total=len(items))


@router.post(
    "/apps/review/{version_id}/approve",
    response_model=AppVersionOut,
    dependencies=[Depends(require_permission("app.review", "app"))],
)
async def review_approve(
    version_id: str,
    body: ReviewDecision | None = None,
    db: AsyncSession = TDbSession,
    tenant_id: str = TTenantId,
    user_id: str = TUserId,
) -> AppVersionOut:
    """Approve a pending version → published (sets app.current_version_id)."""
    svc = AppService(db, tenant_id=tenant_id, user_id=user_id)
    note = body.note if body is not None else None
    ver = await svc.approve_version(version_id, note=note)
    return AppVersionOut.model_validate(ver)


@router.post(
    "/apps/review/{version_id}/reject",
    response_model=AppVersionOut,
    dependencies=[Depends(require_permission("app.review", "app"))],
)
async def review_reject(
    version_id: str,
    body: ReviewDecision,
    db: AsyncSession = TDbSession,
    tenant_id: str = TTenantId,
    user_id: str = TUserId,
) -> AppVersionOut:
    """Reject a pending version → rejected (note required)."""
    svc = AppService(db, tenant_id=tenant_id, user_id=user_id)
    ver = await svc.reject_version(version_id, note=body.note)
    return AppVersionOut.model_validate(ver)


# ---------------------------------------------------------------------------
# Apps
# ---------------------------------------------------------------------------
@router.get(
    "/apps",
    response_model=ListPage,
    dependencies=[Depends(require_permission("app.read", "app"))],
)
async def list_apps(
    scope: str = Query("published", pattern="^(mine|published)$"),
    tag: list[str] | None = Query(
        default=None,
        description="Filter by tag (repeatable; OR semantics - Apps with any match returned).",
    ),
    db: AsyncSession = TDbSession,
    tenant_id: str = TTenantId,
    user_id: str = TUserId,
) -> ListPage:
    """List visible Apps - ``published`` (tenant shelf, default) or ``mine``.

    ``?tag=foo&tag=bar`` filters to Apps whose ``tags`` contain any of the
    given values (OR).  Omitted -> no tag filter.
    """
    svc = AppService(db, tenant_id=tenant_id, user_id=user_id)
    apps = await svc.list_apps(scope=scope, tags=tag)
    items = [AppOut.model_validate(a).model_dump(mode="json") for a in apps]
    return ListPage(items=items, total=len(items))


@router.post(
    "/apps",
    response_model=AppOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("app.create", "app"))],
)
async def create_app(
    body: AppCreate,
    db: AsyncSession = TDbSession,
    tenant_id: str = TTenantId,
    user_id: str = TUserId,
) -> AppOut:
    """Create an App + its initial draft version atomically."""
    svc = AppService(db, tenant_id=tenant_id, user_id=user_id)
    app = await svc.create_app(
        name=body.name,
        description=body.description,
        icon=body.icon,
        visibility=body.visibility.value,
        tags=body.tags,
        version=body.version,
    )
    return AppOut.model_validate(app)


@router.get(
    "/apps/{app_id}",
    response_model=AppOut,
    dependencies=[Depends(require_permission("app.read", "app"))],
)
async def get_app(
    app_id: str,
    db: AsyncSession = TDbSession,
    tenant_id: str = TTenantId,
    user_id: str = TUserId,
    reviewer: bool = TIsReviewer,
) -> AppOut:
    """App detail — author/admin see all versions; others see published current.

    A private App is the author's alone: to anyone else (bar reviewers) it does
    not exist, so the BFF run path, which reads through here, can't run it.
    """
    svc = AppService(db, tenant_id=tenant_id, user_id=user_id)
    app = await svc.get_visible_app(app_id, is_reviewer=reviewer)
    out = AppOut.model_validate(app)
    if app.author_id != user_id and not reviewer:
        # Non-owner: expose only the published current version.
        out.versions = [
            v for v in out.versions
            if v.id == app.current_version_id and v.status == "published"
        ]
    return out


@router.put(
    "/apps/{app_id}",
    response_model=AppOut,
    dependencies=[Depends(require_permission("app.update", "app"))],
)
async def update_app(
    app_id: str,
    body: AppUpdate,
    db: AsyncSession = TDbSession,
    tenant_id: str = TTenantId,
    user_id: str = TUserId,
    reviewer: bool = TIsReviewer,
) -> AppOut:
    svc = AppService(db, tenant_id=tenant_id, user_id=user_id)
    app = await svc.update_app(
        app_id,
        is_reviewer=reviewer,
        name=body.name,
        description=body.description,
        icon=body.icon,
        visibility=body.visibility.value if body.visibility is not None else None,
        tags=body.tags,
    )
    return AppOut.model_validate(app)


@router.delete(
    "/apps/{app_id}",
    status_code=204,
    dependencies=[Depends(require_permission("app.update", "app"))],
)
async def delete_app(
    app_id: str,
    db: AsyncSession = TDbSession,
    tenant_id: str = TTenantId,
    user_id: str = TUserId,
    reviewer: bool = TIsReviewer,
) -> Response:
    svc = AppService(db, tenant_id=tenant_id, user_id=user_id)
    await svc.delete_app(app_id, is_reviewer=reviewer)
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Versions
# ---------------------------------------------------------------------------
@router.post(
    "/apps/{app_id}/versions",
    response_model=AppVersionOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("app.update", "app"))],
)
async def create_version(
    app_id: str,
    body: AppVersionCreate,
    db: AsyncSession = TDbSession,
    tenant_id: str = TTenantId,
    user_id: str = TUserId,
    reviewer: bool = TIsReviewer,
) -> AppVersionOut:
    svc = AppService(db, tenant_id=tenant_id, user_id=user_id)
    ver = await svc.create_version(app_id, is_reviewer=reviewer, body=body)
    return AppVersionOut.model_validate(ver)


@router.put(
    "/apps/{app_id}/versions/{version_id}",
    response_model=AppVersionOut,
    dependencies=[Depends(require_permission("app.update", "app"))],
)
async def update_version(
    app_id: str,
    version_id: str,
    body: AppVersionCreate,
    db: AsyncSession = TDbSession,
    tenant_id: str = TTenantId,
    user_id: str = TUserId,
    reviewer: bool = TIsReviewer,
) -> AppVersionOut:
    """Edit a draft version (rejected if the version is not in draft)."""
    svc = AppService(db, tenant_id=tenant_id, user_id=user_id)
    ver = await svc.update_version(app_id, version_id, is_reviewer=reviewer, body=body)
    return AppVersionOut.model_validate(ver)


@router.post(
    "/apps/{app_id}/versions/{version_id}/submit",
    response_model=AppVersionOut,
    dependencies=[Depends(require_permission("app.update", "app"))],
)
async def submit_version(
    app_id: str,
    version_id: str,
    db: AsyncSession = TDbSession,
    tenant_id: str = TTenantId,
    user_id: str = TUserId,
    reviewer: bool = TIsReviewer,
) -> AppVersionOut:
    """draft → pending_review."""
    svc = AppService(db, tenant_id=tenant_id, user_id=user_id)
    ver = await svc.submit_version(app_id, version_id, is_reviewer=reviewer)
    return AppVersionOut.model_validate(ver)


@router.post(
    "/apps/{app_id}/versions/{version_id}/publish",
    response_model=AppVersionOut,
    dependencies=[Depends(require_permission("app.publish", "app"))],
)
async def publish_version(
    app_id: str,
    version_id: str,
    db: AsyncSession = TDbSession,
    tenant_id: str = TTenantId,
    user_id: str = TUserId,
    reviewer: bool = TIsReviewer,
) -> AppVersionOut:
    """Direct publish — sets app.current_version_id (visibility unchanged)."""
    svc = AppService(db, tenant_id=tenant_id, user_id=user_id)
    ver = await svc.publish_version(app_id, version_id, is_reviewer=reviewer)
    return AppVersionOut.model_validate(ver)


# ---------------------------------------------------------------------------
# Internal — resolve published harness for a run (service-account auth).
# Mirrors skill-store's GET /internal/skills internal-scope pattern.
# ---------------------------------------------------------------------------
@router.get(
    "/internal/apps/{app_id}/resolve",
    response_model=AppOut,
    dependencies=[TInternalAuth],
)
async def internal_resolve(
    app_id: str,
    tenant_id: str,
    db: AsyncSession = Depends(internal_db_session),
) -> AppOut:
    """Resolve an App's published harness for a run (BFF run resolver).

    Returns the App with ONLY its published current version in ``versions``.
    """
    svc = AppService(db, tenant_id=tenant_id)
    app, ver = await svc.resolve_published(app_id)
    out = AppOut.model_validate(app)
    out.versions = [AppVersionOut.model_validate(ver)]
    return out
