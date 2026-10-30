"""Service-specific error codes — RFC 9457 Problem Details."""

from __future__ import annotations


class AppRegistryError(Exception):
    def __init__(self, detail: str, *, code: str, status: int = 400) -> None:
        self.detail = detail
        self.code = code
        self.status = status


class AppNotFoundError(AppRegistryError):
    def __init__(self, app_id: str) -> None:
        super().__init__(
            f"App {app_id} not found",
            code="APP.NOT_FOUND",
            status=404,
        )


class AppNameConflictError(AppRegistryError):
    def __init__(self, name: str) -> None:
        super().__init__(
            f"App name {name!r} already exists for tenant",
            code="APP.NAME_CONFLICT",
            status=409,
        )


class AppVersionNotFoundError(AppRegistryError):
    def __init__(self, version_id: str) -> None:
        super().__init__(
            f"App version {version_id!r} not found",
            code="APP.VERSION.NOT_FOUND",
            status=404,
        )


class AppVersionConflictError(AppRegistryError):
    def __init__(self, version: str) -> None:
        super().__init__(
            f"App version {version!r} already exists",
            code="APP.VERSION.CONFLICT",
            status=409,
        )


class AppVersionNotDraftError(AppRegistryError):
    """A draft-only mutation (edit/submit) was attempted on a non-draft version."""

    def __init__(self, status_: str) -> None:
        super().__init__(
            f"App version is in status {status_!r}; only draft versions can be edited",
            code="APP.VERSION.NOT_DRAFT",
            status=409,
        )


class AppVersionNotPendingError(AppRegistryError):
    """An approve/reject was attempted on a version that is not pending_review."""

    def __init__(self, status_: str) -> None:
        super().__init__(
            f"App version is in status {status_!r}; only pending_review versions can be reviewed",
            code="APP.VERSION.NOT_PENDING",
            status=409,
        )


class AppNoPublishedVersionError(AppRegistryError):
    def __init__(self, app_id: str) -> None:
        super().__init__(
            f"App {app_id} has no published version",
            code="APP.NO_PUBLISHED_VERSION",
            status=404,
        )


class AppForbiddenError(AppRegistryError):
    """Service-layer ownership check failed (non-author, non-reviewer)."""

    def __init__(self, detail: str = "Not the author of this App") -> None:
        super().__init__(
            detail,
            code="APP.FORBIDDEN",
            status=403,
        )


class ReviewNoteRequiredError(AppRegistryError):
    def __init__(self) -> None:
        super().__init__(
            "A review note is required to reject an App version",
            code="APP.REVIEW.NOTE_REQUIRED",
            status=422,
        )


class InternalScopeRequiredError(AppRegistryError):
    def __init__(self) -> None:
        super().__init__(
            "Internal endpoint requires service-account scope app.read_internal",
            code="APP.AUTH.INTERNAL_SCOPE_REQUIRED",
            status=403,
        )


class PrivacyForbiddenError(AppRegistryError):
    def __init__(self) -> None:
        super().__init__(
            "Only an administrator may act on another user's data",
            code="APP.PRIVACY.FORBIDDEN",
            status=403,
        )


class PrivacyInvalidSubjectError(AppRegistryError):
    def __init__(self, user_id: str) -> None:
        super().__init__(
            f"Invalid data subject user_id {user_id!r}",
            code="APP.PRIVACY.INVALID_SUBJECT",
            status=422,
        )
