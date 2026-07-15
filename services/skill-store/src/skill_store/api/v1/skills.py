"""Skill + SkillVersion endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, Request, Response, UploadFile, status
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from skill_store.api.authz_guard import require_permission
from skill_store.api.dependencies import (
    TBlobStore,
    TDbSession,
    TInternalAuth,
    TTenantId,
    TUserId,
    internal_db_session,
)
from skill_store.blob.base import BlobStore, make_key
from skill_store.schemas.common import ListPage
from skill_store.schemas.skill import (
    DefaultInstalledSkillOut,
    InstalledSkillOut,
    ResolvedSkillOut,
    SkillCreate,
    SkillFileOut,
    SkillInstallRequest,
    SkillOut,
    SkillVersionOut,
)
from skill_store.services.skill_service import SkillService

router = APIRouter(tags=["Skills"])


@router.get("/skills", response_model=ListPage,
            dependencies=[Depends(require_permission("skill.read", "skill"))])
async def list_skills(
    db: AsyncSession = TDbSession,
    tenant_id: str = TTenantId,
) -> ListPage:
    """Browse list — skills published to the tenant shelf (tenant/public)."""
    svc = SkillService(db, blob=_no_blob(), tenant_id=tenant_id)
    items = await svc.list_skills()
    return ListPage(
        items=[SkillOut.model_validate(s).model_dump(mode="json") for s in items],
        total=len(items),
    )


@router.get("/skills/installed", response_model=ListPage,
            dependencies=[Depends(require_permission("skill.read", "skill"))])
async def list_installed_skills(
    db: AsyncSession = TDbSession,
    tenant_id: str = TTenantId,
    user_id: str = TUserId,
) -> ListPage:
    """The calling user's installed skills (their personal toolbox)."""
    svc = SkillService(db, blob=_no_blob(), tenant_id=tenant_id, user_id=user_id)
    pairs = await svc.list_installed()
    items = []
    for skill, install in pairs:
        entry = InstalledSkillOut.model_validate(
            {
                **SkillOut.model_validate(skill).model_dump(),
                "enabled_by_default": install.enabled_by_default,
                "installed_at": install.installed_at,
            }
        ).model_dump(mode="json")
        items.append(entry)
    return ListPage(items=items, total=len(items))


@router.post(
    "/skills/{skill_id}/install",
    response_model=InstalledSkillOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("skill.read", "skill"))],
)
async def install_skill(
    skill_id: str,
    body: SkillInstallRequest | None = None,
    db: AsyncSession = TDbSession,
    tenant_id: str = TTenantId,
    user_id: str = TUserId,
) -> InstalledSkillOut:
    """Install a tenant skill into the calling user's toolbox (idempotent)."""
    svc = SkillService(db, blob=_no_blob(), tenant_id=tenant_id, user_id=user_id)
    enabled = body.enabled_by_default if body is not None else True
    pinned = body.version if body is not None else None
    install = await svc.install_skill(
        skill_id, enabled_by_default=enabled, version=pinned,
    )
    skill = await svc.get_skill(skill_id)
    return InstalledSkillOut.model_validate(
        {
            **SkillOut.model_validate(skill).model_dump(),
            "enabled_by_default": install.enabled_by_default,
            "installed_at": install.installed_at,
        }
    )


@router.delete(
    "/skills/{skill_id}/install",
    status_code=204,
    dependencies=[Depends(require_permission("skill.read", "skill"))],
)
async def uninstall_skill(
    skill_id: str,
    db: AsyncSession = TDbSession,
    tenant_id: str = TTenantId,
    user_id: str = TUserId,
) -> Response:
    """Remove a skill from the calling user's toolbox (no-op if absent)."""
    svc = SkillService(db, blob=_no_blob(), tenant_id=tenant_id, user_id=user_id)
    await svc.uninstall_skill(skill_id)
    return Response(status_code=204)


@router.post(
    "/skills",
    response_model=SkillOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("skill.create", "skill"))],
)
async def create_skill(
    body: SkillCreate,
    request: Request,
    db: AsyncSession = TDbSession,
    tenant_id: str = TTenantId,
) -> SkillOut:
    quota_guard = request.app.state.quota_guard
    await quota_guard.check(tenant_id=tenant_id, dimension="skills", requested=1)

    svc = SkillService(db, blob=_no_blob(), tenant_id=tenant_id)
    skill = await svc.create_skill(
        name=body.name,
        description=body.description,
        owner=body.owner,
        visibility=body.visibility.value,
    )

    await quota_guard.record(
        tenant_id=tenant_id,
        dimension="skills",
        amount=1.0,
        description=f"create skill {skill.id}",
    )
    return SkillOut.model_validate(skill)


@router.get("/skills/{skill_id}", response_model=SkillOut,
            dependencies=[Depends(require_permission("skill.read", "skill"))])
async def get_skill(
    skill_id: str,
    db: AsyncSession = TDbSession,
    tenant_id: str = TTenantId,
) -> SkillOut:
    svc = SkillService(db, blob=_no_blob(), tenant_id=tenant_id)
    skill = await svc.get_skill(skill_id)
    return SkillOut.model_validate(skill)


@router.delete(
    "/skills/{skill_id}",
    status_code=204,
    dependencies=[Depends(require_permission("skill.delete", "skill"))],
)
async def delete_skill(
    skill_id: str,
    db: AsyncSession = TDbSession,
    blob: BlobStore = TBlobStore,
    tenant_id: str = TTenantId,
) -> Response:
    """Delete a skill outright — all versions (blobs + rows) and installs."""
    svc = SkillService(db, blob=blob, tenant_id=tenant_id)
    await svc.delete_skill(skill_id)
    return Response(status_code=204)


@router.post(
    "/skills/{skill_id}/versions",
    response_model=SkillVersionOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("skill.update", "skill"))],
)
async def upload_version(
    skill_id: str,
    version: str = Form(...),
    manifest: str | None = Form(default=None),
    expected_sha256: str | None = Form(default=None),
    file: UploadFile = File(...),
    db: AsyncSession = TDbSession,
    blob: BlobStore = TBlobStore,
    tenant_id: str = TTenantId,
    user_id: str = TUserId,
) -> SkillVersionOut:
    payload = await file.read()
    svc = SkillService(db, blob=blob, tenant_id=tenant_id)
    sv = await svc.upload_version(
        skill_id,
        version=version,
        tarball=payload,
        manifest=manifest,
        expected_sha256=expected_sha256,
        uploaded_by=user_id,
    )
    return SkillVersionOut.model_validate(sv)


@router.get("/skills/{skill_id}/versions", response_model=ListPage,
            dependencies=[Depends(require_permission("skill.read", "skill"))])
async def list_skill_versions(
    skill_id: str,
    db: AsyncSession = TDbSession,
    tenant_id: str = TTenantId,
) -> ListPage:
    """List all versions of a skill (newest upload first)."""
    svc = SkillService(db, blob=_no_blob(), tenant_id=tenant_id)
    versions = await svc.list_versions(skill_id)
    return ListPage(
        items=[SkillVersionOut.model_validate(v).model_dump(mode="json") for v in versions],
        total=len(versions),
    )


@router.get("/skills/{skill_id}/versions/{version}/download",
            dependencies=[Depends(require_permission("skill.read", "skill"))])
async def download_version(
    skill_id: str,
    version: str,
    request: Request,
    db: AsyncSession = TDbSession,
    blob: BlobStore = TBlobStore,
    tenant_id: str = TTenantId,
) -> Response:
    svc = SkillService(db, blob=blob, tenant_id=tenant_id)
    settings = request.app.state.settings
    url = await svc.presign_version_download(
        skill_id, version, ttl_seconds=settings.presign_ttl_seconds,
    )
    return RedirectResponse(url, status_code=307)


@router.delete(
    "/skills/{skill_id}/versions/{version}",
    status_code=204,
    dependencies=[Depends(require_permission("skill.delete", "skill"))],
)
async def delete_version(
    skill_id: str,
    version: str,
    db: AsyncSession = TDbSession,
    blob: BlobStore = TBlobStore,
    tenant_id: str = TTenantId,
) -> Response:
    svc = SkillService(db, blob=blob, tenant_id=tenant_id)
    await svc.delete_version(skill_id, version)
    return Response(status_code=204)


@router.get(
    "/internal/skills",
    response_model=ListPage,
    dependencies=[TInternalAuth],
)
async def internal_list(
    tenant_id: str,
    request: Request,
    names: str | None = None,
    db: AsyncSession = Depends(internal_db_session),
    blob: BlobStore = TBlobStore,
) -> ListPage:
    """For skills_loader — list skills (with versions) for a tenant."""
    svc = SkillService(db, blob=blob, tenant_id=tenant_id)
    settings = request.app.state.settings
    name_list = [n.strip() for n in names.split(",")] if names else None
    pairs = await svc.list_internal(tenant_id, names=name_list)
    # blob_url 存在 DB 里的是内部 storage key 形态(如 s3://...),worker
    # 用 httpx 直拉下载不到。在 internal 端点出口处把它替换成短期预签名 HTTP
    # URL,让 sandbox 里的 skills_loader 可直接 GET 到 tarball。
    items = []
    for skill, versions in pairs:
        entry = SkillOut.model_validate(skill).model_dump(mode="json")
        version_dicts = []
        for v in versions:
            vd = SkillVersionOut.model_validate(v).model_dump(mode="json")
            try:
                key = make_key(tenant_id, v.skill_id, v.version)
                vd["blob_url"] = blob.presign_get(
                    key, ttl_seconds=settings.presign_ttl_seconds,
                )
            except Exception:  # noqa: BLE001 -- 失败保留原 blob_url,worker 端会再 failure-open
                pass
            version_dicts.append(vd)
        entry["versions"] = version_dicts
        items.append(entry)
    return ListPage(items=items, total=len(items))


@router.get(
    "/internal/skills/resolve",
    response_model=ListPage,
    dependencies=[TInternalAuth],
)
async def internal_resolve(
    tenant_id: str,
    user_id: str,
    request: Request,
    names: str | None = None,
    db: AsyncSession = Depends(internal_db_session),
    blob: BlobStore = TBlobStore,
) -> ListPage:
    """Resolve (user, skill name) → concrete (version, sha256, presigned URL).

    The scheduler calls this once per run to learn which skill blobs to
    materialise into its content-addressed host cache before mounting them
    into the sandbox.  The response is a ``ListPage`` of ``ResolvedSkillOut``
    so it shares the wire shape with other ``/internal/*`` listings.

    Skills the user hasn't installed are silently absent from the result —
    callers know what they asked for.  Empty ``names`` → empty list.
    """
    name_list = [n.strip() for n in names.split(",") if n.strip()] if names else []
    if not name_list:
        return ListPage(items=[], total=0)

    svc = SkillService(db, blob=blob, tenant_id=tenant_id, user_id=user_id)
    settings = request.app.state.settings
    resolved = await svc.resolve_for_runtime(
        tenant_id, user_id, name_list, ttl_seconds=settings.presign_ttl_seconds,
    )
    items = [
        ResolvedSkillOut(
            name=r.name,
            version=r.version,
            sha256=r.sha256,
            blob_url=r.blob_url,
            description=r.description,
        ).model_dump(mode="json")
        for r in resolved
    ]
    return ListPage(items=items, total=len(items))


@router.get(
    "/internal/skills/{name}/file",
    response_model=SkillFileOut,
    dependencies=[TInternalAuth],
)
async def internal_read_skill_file(
    name: str,
    tenant_id: str,
    user_id: str,
    path: str = "SKILL.md",
    db: AsyncSession = Depends(internal_db_session),
    blob: BlobStore = TBlobStore,
) -> SkillFileOut:
    """Read a single member of an installed skill's package as UTF-8 text.

    The session-orchestrator's inline mode calls this to pull a skill's
    ``SKILL.md`` body — or a ``references/`` file — into the process on demand,
    without downloading and unpacking the whole tarball.  Version resolution is
    identical to ``/internal/skills/resolve`` (same skill name → same version).

    ``path`` (default ``SKILL.md``) is resolved relative to the skill root (the
    directory containing SKILL.md).  Absolute paths or ``..`` segments are
    rejected (400); a missing member is 404; an oversized member is 413; a
    non-UTF-8 member is 415; an uninstalled / version-less skill is 404.
    """
    svc = SkillService(db, blob=blob, tenant_id=tenant_id, user_id=user_id)
    sv, extracted = await svc.read_skill_file(tenant_id, user_id, name, path)
    return SkillFileOut(
        name=name,
        version=sv.version,
        sha256=sv.sha256,
        path=extracted.path,
        content=extracted.content,
    )


@router.get(
    "/internal/skills/default-installed",
    response_model=ListPage,
    dependencies=[TInternalAuth],
)
async def internal_default_installed(
    tenant_id: str,
    user_id: str,
    db: AsyncSession = Depends(internal_db_session),
    blob: BlobStore = TBlobStore,
) -> ListPage:
    """List the user's default-enabled installed skills (names only).

    The orchestrator calls this when a run request does not explicitly name any
    skills: it takes the user's ``enabled_by_default`` installed skills as the
    default skill set to auto-inject into the run.  Skills the user installed
    with ``enabled_by_default=False`` are opt-in per run and are excluded here.

    The response is a ``ListPage`` of ``DefaultInstalledSkillOut`` (``{"name": ...}``)
    so it shares the wire shape with the other ``/internal/*`` listings.
    """
    svc = SkillService(db, blob=blob, tenant_id=tenant_id, user_id=user_id)
    pairs = await svc.list_installed()
    items = [
        DefaultInstalledSkillOut(name=skill.name).model_dump(mode="json")
        for skill, install in pairs
        if install.enabled_by_default
    ]
    return ListPage(items=items, total=len(items))


# ---------------------------------------------------------------------------
# Listing skills doesn't touch blob storage, but SkillService requires a
# ``BlobStore`` instance.  We provide a sentinel that explodes if used so a
# regression that accidentally calls blob ops from a list endpoint surfaces
# loudly rather than silently picking up the prod store.
# ---------------------------------------------------------------------------
class _UnusedBlob:
    def __getattr__(self, name: str) -> object:
        raise RuntimeError(
            f"BlobStore.{name} invoked on a code path that should not touch storage",
        )


def _no_blob() -> BlobStore:  # pragma: no cover — trivial helper
    return _UnusedBlob()  # type: ignore[return-value]
