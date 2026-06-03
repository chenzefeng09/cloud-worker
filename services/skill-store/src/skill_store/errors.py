"""Service-specific error codes — RFC 9457 Problem Details."""

from __future__ import annotations


class SkillStoreError(Exception):
    def __init__(self, detail: str, *, code: str, status: int = 400) -> None:
        self.detail = detail
        self.code = code
        self.status = status


class SkillNotFoundError(SkillStoreError):
    def __init__(self, skill_id: str) -> None:
        super().__init__(
            f"Skill {skill_id} not found",
            code="SKILL.NOT_FOUND",
            status=404,
        )


class SkillNameConflictError(SkillStoreError):
    def __init__(self, name: str) -> None:
        super().__init__(
            f"Skill name {name!r} already exists for tenant",
            code="SKILL.NAME_CONFLICT",
            status=409,
        )


class VersionNotFoundError(SkillStoreError):
    def __init__(self, version: str) -> None:
        super().__init__(
            f"Skill version {version!r} not found",
            code="SKILL.VERSION.NOT_FOUND",
            status=404,
        )


class VersionConflictError(SkillStoreError):
    def __init__(self, version: str) -> None:
        super().__init__(
            f"Skill version {version!r} already exists",
            code="SKILL.VERSION.CONFLICT",
            status=409,
        )


class SkillSha256MismatchError(SkillStoreError):
    def __init__(self) -> None:
        super().__init__(
            "Uploaded tarball sha256 does not match expected_sha256",
            code="SKILL.SHA256_MISMATCH",
            status=422,
        )


class InternalScopeRequiredError(SkillStoreError):
    def __init__(self) -> None:
        super().__init__(
            "Internal endpoint requires service-account scope skill.read_internal",
            code="SKILL.AUTH.INTERNAL_SCOPE_REQUIRED",
            status=403,
        )
