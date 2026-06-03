"""Service-layer tests for SkillService."""

from __future__ import annotations

import hashlib
import json

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from skill_store.blob.memory import InMemoryBlobStore
from skill_store.errors import (
    SkillNameConflictError,
    SkillNotFoundError,
    SkillSha256MismatchError,
    VersionConflictError,
    VersionNotFoundError,
)
from skill_store.services.skill_service import SkillService
from tests.conftest import TEST_TENANT, TEST_TENANT_OTHER


@pytest.fixture
def svc(session: AsyncSession) -> SkillService:
    return SkillService(session, blob=InMemoryBlobStore(), tenant_id=TEST_TENANT)


@pytest.mark.asyncio
async def test_create_skill_assigns_tenant(svc: SkillService) -> None:
    skill = await svc.create_skill(
        name="poet", description="writes poetry", owner=None, visibility="private",
    )
    assert skill.tenant_id == TEST_TENANT
    assert skill.id.startswith("skl_")


@pytest.mark.asyncio
async def test_create_duplicate_raises(svc: SkillService) -> None:
    await svc.create_skill(name="poet", description=None, owner=None, visibility="private")
    with pytest.raises(SkillNameConflictError):
        await svc.create_skill(name="poet", description=None, owner=None, visibility="private")


@pytest.mark.asyncio
async def test_get_missing_skill_raises(svc: SkillService) -> None:
    with pytest.raises(SkillNotFoundError):
        await svc.get_skill("skl_unknown")


@pytest.mark.asyncio
async def test_upload_version_persists_blob_and_row(svc: SkillService) -> None:
    skill = await svc.create_skill(
        name="poet", description=None, owner=None, visibility="private",
    )
    data = b"tarball-bytes"
    sv = await svc.upload_version(
        skill.id,
        version="1.0.0",
        tarball=data,
        manifest={"name": "poet", "version": "1.0.0"},
        expected_sha256=None,
        uploaded_by="me",
    )
    assert sv.sha256 == hashlib.sha256(data).hexdigest()
    assert sv.blob_url.startswith("memory://tenant/")


@pytest.mark.asyncio
async def test_upload_version_sha256_mismatch_rejected(svc: SkillService) -> None:
    skill = await svc.create_skill(
        name="poet", description=None, owner=None, visibility="private",
    )
    with pytest.raises(SkillSha256MismatchError):
        await svc.upload_version(
            skill.id,
            version="1.0.0",
            tarball=b"abc",
            manifest=None,
            expected_sha256="deadbeef",
            uploaded_by=None,
        )


@pytest.mark.asyncio
async def test_upload_version_accepts_json_string_manifest(svc: SkillService) -> None:
    skill = await svc.create_skill(
        name="poet", description=None, owner=None, visibility="private",
    )
    sv = await svc.upload_version(
        skill.id,
        version="1.0.0",
        tarball=b"x",
        manifest=json.dumps({"k": "v"}),
        expected_sha256=None,
        uploaded_by=None,
    )
    assert sv.manifest == {"k": "v"}


@pytest.mark.asyncio
async def test_upload_duplicate_version_raises(svc: SkillService) -> None:
    skill = await svc.create_skill(
        name="poet", description=None, owner=None, visibility="private",
    )
    await svc.upload_version(
        skill.id,
        version="1.0.0",
        tarball=b"x",
        manifest=None,
        expected_sha256=None,
        uploaded_by=None,
    )
    with pytest.raises(VersionConflictError):
        await svc.upload_version(
            skill.id,
            version="1.0.0",
            tarball=b"y",
            manifest=None,
            expected_sha256=None,
            uploaded_by=None,
        )


@pytest.mark.asyncio
async def test_presign_unknown_version_raises(svc: SkillService) -> None:
    skill = await svc.create_skill(
        name="poet", description=None, owner=None, visibility="private",
    )
    with pytest.raises(VersionNotFoundError):
        await svc.presign_version_download(skill.id, "0.0.1")


@pytest.mark.asyncio
async def test_delete_version_drops_row_and_blob(svc: SkillService) -> None:
    skill = await svc.create_skill(
        name="poet", description=None, owner=None, visibility="private",
    )
    await svc.upload_version(
        skill.id,
        version="1.0.0",
        tarball=b"x",
        manifest=None,
        expected_sha256=None,
        uploaded_by=None,
    )
    await svc.delete_version(skill.id, "1.0.0")
    with pytest.raises(VersionNotFoundError):
        await svc.get_version(skill.id, "1.0.0")
    # Blob bucket emptied — only InMemoryBlobStore tracks this.
    assert svc._blob.size == 0  # type: ignore[attr-defined] # noqa: SLF001


@pytest.mark.asyncio
async def test_list_internal_filters_by_names(session: AsyncSession) -> None:
    svc = SkillService(session, blob=InMemoryBlobStore(), tenant_id=TEST_TENANT)
    await svc.create_skill(name="a", description=None, owner=None, visibility="private")
    await svc.create_skill(name="b", description=None, owner=None, visibility="private")
    other = SkillService(session, blob=InMemoryBlobStore(), tenant_id=TEST_TENANT_OTHER)
    await other.create_skill(
        name="a", description=None, owner=None, visibility="private",
    )

    rows = await svc.list_internal(TEST_TENANT, names=["a"])
    assert [s.name for s, _ in rows] == ["a"]
    assert rows[0][0].tenant_id == TEST_TENANT
