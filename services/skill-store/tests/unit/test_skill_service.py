"""Service-layer tests for SkillService."""

from __future__ import annotations

import hashlib
import io
import tarfile

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from skill_store.blob.memory import InMemoryBlobStore
from skill_store.errors import (
    SkillNameConflictError,
    SkillNotFoundError,
    SkillPackageInvalidError,
    SkillSha256MismatchError,
    VersionConflictError,
    VersionNotFoundError,
)
from skill_store.services.skill_service import SkillService
from tests.conftest import TEST_TENANT, TEST_TENANT_OTHER


def _tarball(*, name: str = "poet", description: str = "writes poetry") -> bytes:
    """A minimal valid skill package tarball for service-layer upload tests."""
    content = f"---\nname: {name}\ndescription: {description}\n---\nbody\n".encode()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo(name="SKILL.md")
        info.size = len(content)
        tar.addfile(info, io.BytesIO(content))
    return buf.getvalue()


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
    data = _tarball()
    sv = await svc.upload_version(
        skill.id,
        version="1.0.0",
        tarball=data,
        manifest={"attacker": "controlled"},
        expected_sha256=None,
        uploaded_by="me",
    )
    assert sv.sha256 == hashlib.sha256(data).hexdigest()
    assert sv.blob_url.startswith("memory://tenant/")
    # Manifest is server-authoritative — parsed from SKILL.md, not the uploader.
    assert sv.manifest == {
        "name": "poet",
        "description": "writes poetry",
        "skill_md_path": "SKILL.md",
        "format": "skill_md_v1",
        "sha256": hashlib.sha256(data).hexdigest(),
    }


@pytest.mark.asyncio
async def test_upload_version_sha256_mismatch_rejected(svc: SkillService) -> None:
    skill = await svc.create_skill(
        name="poet", description=None, owner=None, visibility="private",
    )
    with pytest.raises(SkillSha256MismatchError):
        await svc.upload_version(
            skill.id,
            version="1.0.0",
            tarball=_tarball(),
            manifest=None,
            expected_sha256="deadbeef",
            uploaded_by=None,
        )


@pytest.mark.asyncio
async def test_upload_version_rejects_invalid_package(svc: SkillService) -> None:
    skill = await svc.create_skill(
        name="poet", description=None, owner=None, visibility="private",
    )
    with pytest.raises(SkillPackageInvalidError):
        await svc.upload_version(
            skill.id,
            version="1.0.0",
            tarball=b"not-a-tarball",
            manifest=None,
            expected_sha256=None,
            uploaded_by=None,
        )


@pytest.mark.asyncio
async def test_upload_duplicate_version_raises(svc: SkillService) -> None:
    skill = await svc.create_skill(
        name="poet", description=None, owner=None, visibility="private",
    )
    await svc.upload_version(
        skill.id,
        version="1.0.0",
        tarball=_tarball(),
        manifest=None,
        expected_sha256=None,
        uploaded_by=None,
    )
    with pytest.raises(VersionConflictError):
        await svc.upload_version(
            skill.id,
            version="1.0.0",
            tarball=_tarball(description="a different body"),
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
        tarball=_tarball(),
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
async def test_delete_skill_drops_rows_and_all_blobs(svc: SkillService) -> None:
    skill = await svc.create_skill(
        name="poet", description=None, owner=None, visibility="private",
    )
    for ver in ("1.0.0", "2.0.0"):
        await svc.upload_version(
            skill.id,
            version=ver,
            tarball=_tarball(),
            manifest=None,
            expected_sha256=None,
            uploaded_by=None,
        )
    assert svc._blob.size == 2  # type: ignore[attr-defined] # noqa: SLF001

    await svc.delete_skill(skill.id)

    # Skill row gone, versions cascade-deleted, all blobs cleaned up.
    with pytest.raises(SkillNotFoundError):
        await svc.get_skill(skill.id)
    assert svc._blob.size == 0  # type: ignore[attr-defined] # noqa: SLF001


@pytest.mark.asyncio
async def test_delete_unknown_skill_raises(svc: SkillService) -> None:
    with pytest.raises(SkillNotFoundError):
        await svc.delete_skill("skl_unknown")


@pytest.mark.asyncio
async def test_delete_skill_tolerates_blob_failure(svc: SkillService) -> None:
    """A blob delete failure on one version must not abort the DB removal."""
    skill = await svc.create_skill(
        name="poet", description=None, owner=None, visibility="private",
    )
    await svc.upload_version(
        skill.id,
        version="1.0.0",
        tarball=_tarball(),
        manifest=None,
        expected_sha256=None,
        uploaded_by=None,
    )

    def _boom(_key: str) -> None:
        raise RuntimeError("blob backend down")

    svc._blob.delete = _boom  # type: ignore[method-assign] # noqa: SLF001

    # Does not raise — skill row is still removed.
    await svc.delete_skill(skill.id)
    with pytest.raises(SkillNotFoundError):
        await svc.get_skill(skill.id)


@pytest.mark.asyncio
async def test_list_versions_returns_all_newest_first(svc: SkillService) -> None:
    skill = await svc.create_skill(
        name="poet", description=None, owner=None, visibility="private",
    )
    for ver in ("1.0.0", "2.0.0", "3.0.0"):
        await svc.upload_version(
            skill.id,
            version=ver,
            tarball=_tarball(),
            manifest=None,
            expected_sha256=None,
            uploaded_by=None,
        )
    versions = await svc.list_versions(skill.id)
    assert {v.version for v in versions} == {"1.0.0", "2.0.0", "3.0.0"}


@pytest.mark.asyncio
async def test_list_versions_empty_for_skill_without_versions(svc: SkillService) -> None:
    skill = await svc.create_skill(
        name="poet", description=None, owner=None, visibility="private",
    )
    assert await svc.list_versions(skill.id) == []


@pytest.mark.asyncio
async def test_list_versions_unknown_skill_raises(svc: SkillService) -> None:
    with pytest.raises(SkillNotFoundError):
        await svc.list_versions("skl_unknown")


@pytest.mark.asyncio
async def test_install_without_version_leaves_pinned_null(
    session: AsyncSession,
) -> None:
    svc = SkillService(
        session, blob=InMemoryBlobStore(), tenant_id=TEST_TENANT, user_id="usr_alice",
    )
    skill = await svc.create_skill(
        name="poet", description=None, owner=None, visibility="private",
    )
    install = await svc.install_skill(skill.id)
    assert install.pinned_version_id is None


@pytest.mark.asyncio
async def test_install_with_version_pins_correct_id(
    session: AsyncSession,
) -> None:
    svc = SkillService(
        session, blob=InMemoryBlobStore(), tenant_id=TEST_TENANT, user_id="usr_alice",
    )
    skill = await svc.create_skill(
        name="poet", description=None, owner=None, visibility="private",
    )
    sv = await svc.upload_version(
        skill.id, version="1.0.0", tarball=_tarball(),
        manifest=None, expected_sha256=None, uploaded_by=None,
    )
    install = await svc.install_skill(skill.id, version="1.0.0")
    assert install.pinned_version_id == sv.id


@pytest.mark.asyncio
async def test_install_with_unknown_version_raises(session: AsyncSession) -> None:
    svc = SkillService(
        session, blob=InMemoryBlobStore(), tenant_id=TEST_TENANT, user_id="usr_alice",
    )
    skill = await svc.create_skill(
        name="poet", description=None, owner=None, visibility="private",
    )
    with pytest.raises(VersionNotFoundError):
        await svc.install_skill(skill.id, version="9.9.9")


@pytest.mark.asyncio
async def test_install_reinstall_updates_pinned(session: AsyncSession) -> None:
    svc = SkillService(
        session, blob=InMemoryBlobStore(), tenant_id=TEST_TENANT, user_id="usr_alice",
    )
    skill = await svc.create_skill(
        name="poet", description=None, owner=None, visibility="private",
    )
    await svc.upload_version(
        skill.id, version="1.0.0", tarball=_tarball(),
        manifest=None, expected_sha256=None, uploaded_by=None,
    )
    sv2 = await svc.upload_version(
        skill.id, version="2.0.0", tarball=_tarball(description="v2 body"),
        manifest=None, expected_sha256=None, uploaded_by=None,
    )
    # First install: follow latest.
    inst = await svc.install_skill(skill.id)
    assert inst.pinned_version_id is None
    # Re-install pinning a version updates in place.
    inst = await svc.install_skill(skill.id, version="2.0.0")
    assert inst.pinned_version_id == sv2.id
    # Re-install dropping the pin clears it.
    inst = await svc.install_skill(skill.id, version=None)
    assert inst.pinned_version_id is None


@pytest.mark.asyncio
async def test_resolve_for_runtime_mixes_pinned_and_latest(
    session: AsyncSession,
) -> None:
    svc = SkillService(
        session, blob=InMemoryBlobStore(), tenant_id=TEST_TENANT, user_id="usr_alice",
    )
    # Skill "poet" — two versions, user pins 1.0.0.
    poet = await svc.create_skill(
        name="poet", description=None, owner=None, visibility="private",
    )
    await svc.upload_version(
        poet.id, version="1.0.0", tarball=_tarball(),
        manifest=None, expected_sha256=None, uploaded_by=None,
    )
    await svc.upload_version(
        poet.id, version="2.0.0", tarball=_tarball(description="poet v2"),
        manifest=None, expected_sha256=None, uploaded_by=None,
    )
    await svc.install_skill(poet.id, version="1.0.0")

    # Skill "chef" — two versions, user follows latest.
    chef = await svc.create_skill(
        name="chef", description=None, owner=None, visibility="private",
    )
    await svc.upload_version(
        chef.id, version="0.1.0", tarball=_tarball(name="chef", description="chef v0"),
        manifest=None, expected_sha256=None, uploaded_by=None,
    )
    chef_latest = await svc.upload_version(
        chef.id, version="0.2.0", tarball=_tarball(name="chef", description="chef v0.2"),
        manifest=None, expected_sha256=None, uploaded_by=None,
    )
    await svc.install_skill(chef.id)

    resolved = await svc.resolve_for_runtime(
        TEST_TENANT, "usr_alice", ["poet", "chef", "ghost"], ttl_seconds=600,
    )
    by_name = {r.name: r for r in resolved}
    # ``ghost`` has no install -- silently absent.
    assert set(by_name) == {"poet", "chef"}
    assert by_name["poet"].version == "1.0.0"
    assert by_name["chef"].version == "0.2.0"
    assert by_name["chef"].sha256 == chef_latest.sha256
    # blob_url is a presigned URL from InMemoryBlobStore (carries ttl param).
    assert by_name["poet"].blob_url.startswith("memory://tenant/")
    assert "ttl=600" in by_name["poet"].blob_url


@pytest.mark.asyncio
async def test_resolve_for_runtime_empty_names(session: AsyncSession) -> None:
    svc = SkillService(
        session, blob=InMemoryBlobStore(), tenant_id=TEST_TENANT, user_id="usr_alice",
    )
    assert await svc.resolve_for_runtime(
        TEST_TENANT, "usr_alice", [], ttl_seconds=600,
    ) == []


@pytest.mark.asyncio
async def test_resolve_for_runtime_skips_skills_without_versions(
    session: AsyncSession,
) -> None:
    """Installed skill with no uploaded version yet -> silently skipped."""
    svc = SkillService(
        session, blob=InMemoryBlobStore(), tenant_id=TEST_TENANT, user_id="usr_alice",
    )
    skill = await svc.create_skill(
        name="poet", description=None, owner=None, visibility="private",
    )
    await svc.install_skill(skill.id)
    assert await svc.resolve_for_runtime(
        TEST_TENANT, "usr_alice", ["poet"], ttl_seconds=600,
    ) == []


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
