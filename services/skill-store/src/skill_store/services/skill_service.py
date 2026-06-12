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

import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from skill_store.blob.base import BlobStore, make_key
from skill_store.domain.models import Skill, SkillVersion, UserInstalledSkill
from skill_store.errors import (
    SkillNameConflictError,
    SkillNotFoundError,
    SkillSha256MismatchError,
    VersionConflictError,
    VersionNotFoundError,
)
from skill_store.services.skill_package import validate_and_parse

# Visibility levels that make a skill visible on the shared shelf (browse list).
# "private" skills are excluded from the shelf. This filter applies ONLY to the
# user-facing browse list — NOT to ``list_internal`` (the worker load path),
# which must load any skill a user actually selected regardless of visibility.
_SHELF_VISIBILITIES = ("tenant", "public")

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ResolvedSkill:
    """A resolved (skill, version) pair the scheduler should materialise.

    ``blob_url`` is a presigned HTTP URL the scheduler fetches once; ``sha256``
    is the content-address used as the cache key.
    """

    name: str
    version: str
    sha256: str
    blob_url: str


class SkillService:
    def __init__(
        self, db: AsyncSession, blob: BlobStore, *, tenant_id: str,
        user_id: str | None = None,
    ) -> None:
        self._db = db
        self._blob = blob
        self._tenant_id = tenant_id
        self._user_id = user_id

    # ----------------------------------------------------------- skills

    async def list_skills(self) -> list[Skill]:
        """Browse list — skills published to the tenant shelf."""
        stmt = (
            select(Skill)
            .where(
                Skill.tenant_id == self._tenant_id,
                Skill.visibility.in_(_SHELF_VISIBILITIES),
            )
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

    async def delete_skill(self, skill_id: str) -> None:
        """Delete a skill and all of its versions.

        DB rows for versions and per-user install records are removed via the
        ORM cascade on ``Skill.versions`` (delete-orphan) and the
        ``UserInstalledSkill`` FK (ondelete CASCADE).  Blobs are NOT covered by
        the DB cascade, so we delete each version's tarball best-effort first.
        A single blob delete failure is logged and skipped rather than aborting
        the whole operation — object storage cleanup is idempotent and any
        leaked object can be GC'd later.
        """
        stmt = (
            select(Skill)
            .where(Skill.id == skill_id, Skill.tenant_id == self._tenant_id)
            .options(selectinload(Skill.versions))
        )
        result = await self._db.execute(stmt)
        skill = result.scalar_one_or_none()
        if skill is None:
            raise SkillNotFoundError(skill_id)

        for sv in list(skill.versions):
            key = make_key(self._tenant_id, skill.id, sv.version)
            try:
                self._blob.delete(key)
            except Exception:  # noqa: BLE001 — tolerate blob leak, keep going
                logger.warning(
                    "blob delete failed during skill delete; leaking object",
                    extra={"skill_id": skill.id, "version": sv.version, "key": key},
                    exc_info=True,
                )

        await self._db.delete(skill)
        await self._db.flush()

    # ------------------------------------------------------- installs

    def _require_user(self) -> str:
        if not self._user_id:
            raise RuntimeError("SkillService was constructed without a user_id")
        return self._user_id

    async def list_installed(self) -> list[tuple[Skill, UserInstalledSkill]]:
        """The calling user's installed skills (their personal toolbox)."""
        user_id = self._require_user()
        stmt = (
            select(Skill, UserInstalledSkill)
            .join(UserInstalledSkill, UserInstalledSkill.skill_id == Skill.id)
            .where(
                UserInstalledSkill.tenant_id == self._tenant_id,
                UserInstalledSkill.user_id == user_id,
            )
            .order_by(Skill.name.asc())
        )
        result = await self._db.execute(stmt)
        return [(row[0], row[1]) for row in result.all()]

    async def install_skill(
        self, skill_id: str, *, enabled_by_default: bool = True,
        version: str | None = None,
    ) -> UserInstalledSkill:
        """Install a skill into the user's toolbox (idempotent on re-install).

        ``version=None`` means "follow latest" (``pinned_version_id`` is left
        NULL).  A non-None ``version`` must match an existing SkillVersion of
        this skill; otherwise ``VersionNotFoundError`` is raised so the API
        layer turns it into a 404.
        """
        user_id = self._require_user()
        skill = await self.get_skill(skill_id)  # 404 if not in tenant

        pinned_version_id: str | None = None
        if version is not None:
            # Resolve the requested version to its id; raises VersionNotFoundError
            # if it doesn't exist for this skill.
            sv = await self.get_version(skill.id, version)
            pinned_version_id = sv.id

        existing = await self._db.execute(
            select(UserInstalledSkill).where(
                UserInstalledSkill.tenant_id == self._tenant_id,
                UserInstalledSkill.user_id == user_id,
                UserInstalledSkill.skill_id == skill.id,
            )
        )
        row = existing.scalar_one_or_none()
        if row is not None:
            row.enabled_by_default = enabled_by_default
            row.pinned_version_id = pinned_version_id
            await self._db.flush()
            return row

        row = UserInstalledSkill(
            tenant_id=self._tenant_id,
            user_id=user_id,
            skill_id=skill.id,
            enabled_by_default=enabled_by_default,
            pinned_version_id=pinned_version_id,
        )
        self._db.add(row)
        await self._db.flush()
        await self._db.refresh(row)
        return row

    async def uninstall_skill(self, skill_id: str) -> None:
        """Remove a skill from the user's toolbox (no-op if not installed)."""
        user_id = self._require_user()
        result = await self._db.execute(
            select(UserInstalledSkill).where(
                UserInstalledSkill.tenant_id == self._tenant_id,
                UserInstalledSkill.user_id == user_id,
                UserInstalledSkill.skill_id == skill_id,
            )
        )
        row = result.scalar_one_or_none()
        if row is not None:
            await self._db.delete(row)
            await self._db.flush()

    # ----------------------------------------------------------- versions

    async def upload_version(
        self,
        skill_id: str,
        *,
        version: str,
        tarball: bytes,
        manifest: dict[str, Any] | str | None,
        expected_sha256: str | None,
        uploaded_by: str | None,
    ) -> SkillVersion:
        skill = await self.get_skill(skill_id)

        # Trust boundary: validate the tarball is a well-formed Claude skill
        # package and derive authoritative metadata from its SKILL.md.  Raises
        # SkillPackageInvalidError (HTTP 400) on any structural/content problem.
        parsed = validate_and_parse(tarball)

        # validate_and_parse computes the sha256 of the raw bytes; reuse it as
        # the canonical hash and still honour a caller-supplied expectation.
        computed_sha = parsed.computed_sha256
        if expected_sha256 and expected_sha256.lower() != computed_sha:
            raise SkillSha256MismatchError()

        # Manifest is server-authoritative — derived from the parsed package,
        # never from the uploader's ``manifest`` form value (which is ignored).
        del manifest
        manifest_dict: dict[str, Any] = {
            "name": parsed.name,
            "description": parsed.description,
            "skill_md_path": parsed.skill_md_path,
            "format": "skill_md_v1",
            "sha256": parsed.computed_sha256,
        }

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

    async def list_versions(self, skill_id: str) -> list[SkillVersion]:
        """All versions of a skill, newest upload first (404 if skill absent)."""
        await self.get_skill(skill_id)  # 404 if not in tenant
        stmt = (
            select(SkillVersion)
            .where(SkillVersion.skill_id == skill_id)
            .order_by(SkillVersion.uploaded_at.desc())
        )
        result = await self._db.execute(stmt)
        return list(result.scalars().all())

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

    async def resolve_for_runtime(
        self,
        tenant_id: str,
        user_id: str,
        names: list[str],
        *,
        ttl_seconds: int,
    ) -> list[ResolvedSkill]:
        """Materialise each requested skill name to a concrete blob to fetch.

        For each install record matching (tenant_id, user_id, skill.name in
        names):

        * ``pinned_version_id`` non-NULL → use that exact SkillVersion.
        * ``pinned_version_id`` NULL    → use the most recently uploaded
          SkillVersion of the skill (by ``uploaded_at`` desc).

        Skill names with no install record are silently omitted — the caller
        knows which names it asked for and can decide what to do about misses.
        """
        if not names:
            return []

        stmt = (
            select(Skill, UserInstalledSkill)
            .join(UserInstalledSkill, UserInstalledSkill.skill_id == Skill.id)
            .where(
                UserInstalledSkill.tenant_id == tenant_id,
                UserInstalledSkill.user_id == user_id,
                Skill.name.in_(names),
            )
            .order_by(Skill.name.asc())
        )
        result = await self._db.execute(stmt)
        installs = list(result.all())

        resolved: list[ResolvedSkill] = []
        for skill, install in installs:
            sv: SkillVersion | None
            if install.pinned_version_id is not None:
                sv_stmt = select(SkillVersion).where(
                    SkillVersion.id == install.pinned_version_id,
                )
                sv = (await self._db.execute(sv_stmt)).scalar_one_or_none()
                # A SET NULL race (pinned row was deleted between query &
                # fetch) would leave sv=None; fall back to latest.
                if sv is None:
                    sv = await self._latest_version(skill.id)
            else:
                sv = await self._latest_version(skill.id)

            if sv is None:
                # Skill has zero versions yet — nothing to materialise.
                continue

            key = make_key(tenant_id, sv.skill_id, sv.version)
            try:
                blob_url = self._blob.presign_get(key, ttl_seconds=ttl_seconds)
            except Exception:  # noqa: BLE001 -- fail-open: scheduler will retry
                logger.warning(
                    "presign_get failed during resolve_for_runtime",
                    extra={"skill_id": sv.skill_id, "version": sv.version, "key": key},
                    exc_info=True,
                )
                blob_url = sv.blob_url

            resolved.append(
                ResolvedSkill(
                    name=skill.name,
                    version=sv.version,
                    sha256=sv.sha256,
                    blob_url=blob_url,
                )
            )
        return resolved

    async def _latest_version(self, skill_id: str) -> SkillVersion | None:
        stmt = (
            select(SkillVersion)
            .where(SkillVersion.skill_id == skill_id)
            .order_by(SkillVersion.uploaded_at.desc())
            .limit(1)
        )
        return (await self._db.execute(stmt)).scalar_one_or_none()

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
