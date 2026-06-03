"""Business logic for skill packages and versions.

The service composes DB writes with blob-store operations.  Order matters:

* On version upload we first persist the blob, *then* the DB row — if the
  DB write fails we delete the blob best-effort (no transactional 2PC
  available against object storage).
* On version delete we drop the DB row first; if the row removal succeeds
  but the blob delete fails we tolerate the leak rather than re-creating
  the row (object storage cleanup is idempotent and can be GC'd later).
"""

from __future__ import annotations

import hashlib
import json

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from skill_store.blob.base import BlobStore, make_key
from skill_store.domain.models import Skill, SkillVersion
from skill_store.errors import (
    SkillNameConflictError,
    SkillNotFoundError,
    SkillSha256MismatchError,
    VersionConflictError,
    VersionNotFoundError,
)


class SkillService:
    def __init__(
        self, db: AsyncSession, blob: BlobStore, *, tenant_id: str,
    ) -> None:
        self._db = db
        self._blob = blob
        self._tenant_id = tenant_id

    # ----------------------------------------------------------- skills

    async def list_skills(self) -> list[Skill]:
        stmt = (
            select(Skill)
            .where(Skill.tenant_id == self._tenant_id)
            .order_by(Skill.name.asc())
        )
        result = await self._db.execute(stmt)
        return list(result.scalars().all())

    async def create_skill(
        self,
        *,
        name: str,
        description: str | None,
        owner: str | None,
        visibility: str,
    ) -> Skill:
        skill = Skill(
            tenant_id=self._tenant_id,
            name=name,
            description=description,
            owner=owner,
            visibility=visibility,
        )
        self._db.add(skill)
        try:
            await self._db.flush()
        except IntegrityError as exc:
            raise SkillNameConflictError(name) from exc
        await self._db.refresh(skill)
        return skill

    async def get_skill(self, skill_id: str) -> Skill:
        stmt = select(Skill).where(
            Skill.id == skill_id, Skill.tenant_id == self._tenant_id,
        )
        result = await self._db.execute(stmt)
        skill = result.scalar_one_or_none()
        if skill is None:
            raise SkillNotFoundError(skill_id)
        return skill

    # ----------------------------------------------------------- versions

    async def upload_version(
        self,
        skill_id: str,
        *,
        version: str,
        tarball: bytes,
        manifest: dict | str | None,
        expected_sha256: str | None,
        uploaded_by: str | None,
    ) -> SkillVersion:
        skill = await self.get_skill(skill_id)

        computed_sha = hashlib.sha256(tarball).hexdigest()
        if expected_sha256 and expected_sha256.lower() != computed_sha:
            raise SkillSha256MismatchError()

        manifest_dict: dict
        if manifest is None:
            manifest_dict = {}
        elif isinstance(manifest, str):
            manifest_dict = json.loads(manifest) if manifest.strip() else {}
        else:
            manifest_dict = manifest

        key = make_key(self._tenant_id, skill.id, version)
        blob_url = self._blob.put(key, tarball)

        sv = SkillVersion(
            skill_id=skill.id,
            version=version,
            manifest=manifest_dict,
            blob_url=blob_url,
            sha256=computed_sha,
            uploaded_by=uploaded_by,
        )
        self._db.add(sv)
        try:
            await self._db.flush()
        except IntegrityError as exc:
            # DB lost the race — clean up the orphaned blob.
            self._blob.delete(key)
            raise VersionConflictError(version) from exc
        await self._db.refresh(sv)
        return sv

    async def get_version(self, skill_id: str, version: str) -> SkillVersion:
        skill = await self.get_skill(skill_id)
        stmt = select(SkillVersion).where(
            SkillVersion.skill_id == skill.id, SkillVersion.version == version,
        )
        result = await self._db.execute(stmt)
        row = result.scalar_one_or_none()
        if row is None:
            raise VersionNotFoundError(version)
        return row

    async def presign_version_download(
        self, skill_id: str, version: str, *, ttl_seconds: int = 600,
    ) -> str:
        sv = await self.get_version(skill_id, version)
        key = make_key(self._tenant_id, sv.skill_id, sv.version)
        return self._blob.presign_get(key, ttl_seconds=ttl_seconds)

    async def delete_version(self, skill_id: str, version: str) -> None:
        sv = await self.get_version(skill_id, version)
        key = make_key(self._tenant_id, sv.skill_id, sv.version)
        await self._db.delete(sv)
        await self._db.flush()
        self._blob.delete(key)

    # ----------------------------------------------------------- internal

    async def list_internal(
        self, tenant_id: str, names: list[str] | None = None,
    ) -> list[tuple[Skill, list[SkillVersion]]]:
        stmt = (
            select(Skill)
            .where(Skill.tenant_id == tenant_id)
            .options(selectinload(Skill.versions))
            .order_by(Skill.name.asc())
        )
        if names:
            stmt = stmt.where(Skill.name.in_(names))
        result = await self._db.execute(stmt)
        skills = list(result.scalars().all())
        return [(s, list(s.versions)) for s in skills]
