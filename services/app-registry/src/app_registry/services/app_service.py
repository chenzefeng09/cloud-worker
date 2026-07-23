"""Business logic for Apps, versions, and the review state machine.

All reads are tenant-scoped (RLS also enforces this at the DB layer).  The
review state machine is::

    draft ──submit──▶ pending_review ──approve──▶ published
                            └──reject──▶ rejected

Only ``draft`` versions are editable; every other status is immutable.  An
author edits a published App by creating a *new* draft version.  ``approve``
(and direct ``publish``) sets ``app.current_version_id`` and bumps
``app.visibility`` to ``tenant`` so the version becomes shelf-visible.

Ownership: ``author_id == user_id`` is enforced for App-owner mutations
(update / new version / edit draft / submit / publish).  Callers holding the
``app.review`` permission (admins) bypass this check — they pass
``is_reviewer=True``.  This is a service-layer check ON TOP of the route-level
authz action guard.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app_registry.domain.models import App, AppVersion, utcnow
from app_registry.errors import (
    AppForbiddenError,
    AppNameConflictError,
    AppNoPublishedVersionError,
    AppNotFoundError,
    AppVersionConflictError,
    AppVersionNotDraftError,
    AppVersionNotFoundError,
    AppVersionNotPendingError,
    ReviewNoteRequiredError,
)
from app_registry.schemas.app import AppVersionCreate

# Visibility levels that put an App on the shared tenant shelf (published list).
_SHELF_VISIBILITIES = ("tenant",)


class AppService:
    def __init__(
        self, db: AsyncSession, *, tenant_id: str, user_id: str | None = None,
    ) -> None:
        self._db = db
        self._tenant_id = tenant_id
        self._user_id = user_id

    # --------------------------------------------------------------- helpers

    def _require_user(self) -> str:
        if not self._user_id:
            raise RuntimeError("AppService was constructed without a user_id")
        return self._user_id

    def _assert_owner_or_reviewer(self, app: App, *, is_reviewer: bool) -> None:
        if is_reviewer:
            return
        if app.author_id != self._require_user():
            raise AppForbiddenError()

    async def _next_version(self, app_id: str) -> str:
        """Return the next monotonically increasing integer version for an App."""
        stmt = select(AppVersion.version).where(
            AppVersion.app_id == app_id,
            AppVersion.tenant_id == self._tenant_id,
        )
        result = await self._db.execute(stmt)
        versions = [str(v) for v in result.scalars().all()]
        max_numeric = 0
        for value in versions:
            try:
                max_numeric = max(max_numeric, int(value))
            except ValueError:
                continue
        if max_numeric > 0:
            return str(max_numeric + 1)
        return str(len(versions) + 1)

    @staticmethod
    def _version_harness_kwargs(body: AppVersionCreate) -> dict[str, object]:
        return {
            "system_prompt": body.system_prompt,
            "user_prompt_template": body.user_prompt_template,
            "model": body.model,
            "reasoning_effort": body.reasoning_effort,
            "sampling_params": body.sampling_params.model_dump(exclude_none=True),
            "forced_skills": list(body.forced_skills),
            "forced_mcp_server_ids": list(body.forced_mcp_server_ids),
            "input_schema": [f.model_dump(mode="json") for f in body.input_schema],
        }

    # ----------------------------------------------------------------- apps

    async def list_apps(self, *, scope: str, tags: list[str] | None = None) -> list[App]:
        """``scope='mine'`` -> caller's own Apps (any status);
        ``scope='published'`` -> tenant-shelf Apps with a published current
        version (the homepage list).

        ``tags`` (optional, OR semantics): only return Apps whose ``tags``
        array contains at least one of the given values.  Filtering happens
        in Python rather than via a JSONB operator (``?|`` / ``@>``) so the
        same code path works against the SQLite test DB and Postgres prod -
        the list is already unpaginated + tenant-scoped, so an in-memory
        filter is consistent with the existing read pattern.
        """
        stmt = (
            select(App)
            .where(App.tenant_id == self._tenant_id)
            .options(selectinload(App.versions))
            .order_by(App.name.asc())
        )
        if scope == "mine":
            stmt = stmt.where(App.author_id == self._require_user())
        else:  # published
            stmt = stmt.where(
                App.visibility.in_(_SHELF_VISIBILITIES),
                App.current_version_id.is_not(None),
            )
        result = await self._db.execute(stmt)
        apps = list(result.scalars().all())
        if tags:
            wanted = {t for t in tags if t}
            if wanted:
                apps = [a for a in apps if wanted.intersection(a.tags)]
        return apps

    async def create_app(self, *, name: str, description: str | None, icon: str | None,
                         visibility: str, version: AppVersionCreate,
                         tags: list[str] | None = None) -> App:
        """Create an App and its initial draft version atomically."""
        user_id = self._require_user()
        app = App(
            tenant_id=self._tenant_id,
            name=name,
            description=description,
            icon=icon,
            author_id=user_id,
            visibility=visibility,
            tags=list(tags) if tags is not None else [],
        )
        self._db.add(app)
        try:
            await self._db.flush()
        except IntegrityError as exc:
            raise AppNameConflictError(name) from exc

        ver = AppVersion(
            app_id=app.id,
            tenant_id=self._tenant_id,
            version=await self._next_version(app.id),
            status="draft",
            created_by=user_id,
            **self._version_harness_kwargs(version),
        )
        self._db.add(ver)
        await self._db.flush()
        await self._db.refresh(app)
        return await self.get_app(app.id)

    async def get_app(self, app_id: str) -> App:
        stmt = (
            select(App)
            .where(App.id == app_id, App.tenant_id == self._tenant_id)
            .options(selectinload(App.versions))
        )
        result = await self._db.execute(stmt)
        app = result.scalar_one_or_none()
        if app is None:
            raise AppNotFoundError(app_id)
        return app

    async def update_app(self, app_id: str, *, is_reviewer: bool, name: str | None,
                         description: str | None, icon: str | None,
                         visibility: str | None,
                         tags: list[str] | None = None) -> App:
        app = await self.get_app(app_id)
        self._assert_owner_or_reviewer(app, is_reviewer=is_reviewer)
        if name is not None:
            app.name = name
        if description is not None:
            app.description = description
        if icon is not None:
            app.icon = icon
        if visibility is not None:
            app.visibility = visibility
        if tags is not None:
            # None = leave unchanged; an explicit list (incl. []) replaces.
            app.tags = list(tags)
        try:
            await self._db.flush()
        except IntegrityError as exc:
            raise AppNameConflictError(name or app.name) from exc
        await self._db.refresh(app)
        return await self.get_app(app_id)

    async def delete_app(self, app_id: str, *, is_reviewer: bool) -> None:
        app = await self.get_app(app_id)
        self._assert_owner_or_reviewer(app, is_reviewer=is_reviewer)
        await self._db.delete(app)
        await self._db.flush()

    # ------------------------------------------------------------- versions

    async def get_version(self, app_id: str, version_id: str) -> AppVersion:
        app = await self.get_app(app_id)
        stmt = select(AppVersion).where(
            AppVersion.id == version_id,
            AppVersion.app_id == app.id,
            AppVersion.tenant_id == self._tenant_id,
        )
        result = await self._db.execute(stmt)
        ver = result.scalar_one_or_none()
        if ver is None:
            raise AppVersionNotFoundError(version_id)
        return ver

    async def _get_version_by_id(self, version_id: str) -> AppVersion:
        """Tenant-scoped version lookup without knowing the app_id (review queue)."""
        stmt = select(AppVersion).where(
            AppVersion.id == version_id,
            AppVersion.tenant_id == self._tenant_id,
        )
        result = await self._db.execute(stmt)
        ver = result.scalar_one_or_none()
        if ver is None:
            raise AppVersionNotFoundError(version_id)
        return ver

    async def create_version(self, app_id: str, *, is_reviewer: bool,
                             body: AppVersionCreate) -> AppVersion:
        """Add a new draft version to an existing App."""
        app = await self.get_app(app_id)
        self._assert_owner_or_reviewer(app, is_reviewer=is_reviewer)
        ver = AppVersion(
            app_id=app.id,
            tenant_id=self._tenant_id,
            version=await self._next_version(app.id),
            status="draft",
            created_by=self._require_user(),
            **self._version_harness_kwargs(body),
        )
        self._db.add(ver)
        try:
            await self._db.flush()
        except IntegrityError as exc:
            raise AppVersionConflictError(ver.version) from exc
        await self._db.refresh(ver)
        return ver

    async def update_version(self, app_id: str, version_id: str, *, is_reviewer: bool,
                             body: AppVersionCreate) -> AppVersion:
        """Edit a draft version in place (rejected if status != draft)."""
        app = await self.get_app(app_id)
        self._assert_owner_or_reviewer(app, is_reviewer=is_reviewer)
        ver = await self.get_version(app_id, version_id)
        if ver.status != "draft":
            raise AppVersionNotDraftError(ver.status)
        for field, value in self._version_harness_kwargs(body).items():
            setattr(ver, field, value)
        try:
            await self._db.flush()
        except IntegrityError as exc:
            raise AppVersionConflictError(ver.version) from exc
        await self._db.refresh(ver)
        return ver

    # --------------------------------------------------------- state machine

    def _set_status(self, ver: AppVersion, new_status: str, *, actor: str) -> None:
        """Single internal transition point; stamps the review trail."""
        now = utcnow()
        if new_status == "pending_review":
            ver.submitted_by = actor
            ver.submitted_at = now
        elif new_status in ("published", "rejected"):
            ver.reviewed_by = actor
            ver.reviewed_at = now
        ver.status = new_status

    async def submit_version(self, app_id: str, version_id: str, *,
                             is_reviewer: bool) -> AppVersion:
        app = await self.get_app(app_id)
        self._assert_owner_or_reviewer(app, is_reviewer=is_reviewer)
        ver = await self.get_version(app_id, version_id)
        if ver.status != "draft":
            raise AppVersionNotDraftError(ver.status)
        self._set_status(ver, "pending_review", actor=self._require_user())
        await self._db.flush()
        await self._db.refresh(ver)
        return ver

    async def publish_version(self, app_id: str, version_id: str, *,
                              is_reviewer: bool) -> AppVersion:
        """Direct publish (admin-built Apps / app.publish holders).

        Publishes any draft|pending_review version, sets the app pointer and
        bumps visibility to ``tenant``.
        """
        app = await self.get_app(app_id)
        self._assert_owner_or_reviewer(app, is_reviewer=is_reviewer)
        ver = await self.get_version(app_id, version_id)
        if ver.status not in ("draft", "pending_review"):
            raise AppVersionNotDraftError(ver.status)
        self._set_status(ver, "published", actor=self._require_user())
        app.current_version_id = ver.id
        app.visibility = "tenant"
        await self._db.flush()
        await self._db.refresh(ver)
        return ver

    # ------------------------------------------------------------- review

    async def list_pending(self) -> list[tuple[AppVersion, App]]:
        """Admin review queue — pending_review versions with their App."""
        stmt = (
            select(AppVersion, App)
            .join(App, App.id == AppVersion.app_id)
            .where(
                AppVersion.tenant_id == self._tenant_id,
                AppVersion.status == "pending_review",
            )
            .order_by(AppVersion.submitted_at.asc())
        )
        result = await self._db.execute(stmt)
        return [(row[0], row[1]) for row in result.all()]

    async def approve_version(self, version_id: str, *, note: str | None) -> AppVersion:
        ver = await self._get_version_by_id(version_id)
        if ver.status != "pending_review":
            raise AppVersionNotPendingError(ver.status)
        app = await self.get_app(ver.app_id)
        self._set_status(ver, "published", actor=self._require_user())
        if note:
            ver.review_note = note
        app.current_version_id = ver.id
        app.visibility = "tenant"
        await self._db.flush()
        await self._db.refresh(ver)
        return ver

    async def reject_version(self, version_id: str, *, note: str | None) -> AppVersion:
        if not note or not note.strip():
            raise ReviewNoteRequiredError()
        ver = await self._get_version_by_id(version_id)
        if ver.status != "pending_review":
            raise AppVersionNotPendingError(ver.status)
        self._set_status(ver, "rejected", actor=self._require_user())
        ver.review_note = note
        await self._db.flush()
        await self._db.refresh(ver)
        return ver

    # ------------------------------------------------------------- internal

    async def resolve_published(self, app_id: str) -> tuple[App, AppVersion]:
        """Resolve an App's currently-published harness for a run (BFF/internal)."""
        app = await self.get_app(app_id)
        if not app.current_version_id:
            raise AppNoPublishedVersionError(app_id)
        ver = await self.get_version(app_id, app.current_version_id)
        if ver.status != "published":
            raise AppNoPublishedVersionError(app_id)
        return app, ver
