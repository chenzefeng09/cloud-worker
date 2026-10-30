"""BFF admin — the personal-data breach register (GDPR Art. 33 / 34).

Kept in the BFF's own database (``breach_records``) rather than proxied:
foundation security-ops tracks *security incidents*, while this register is the
controller's GDPR record of a breach of personal data — when the controller
became aware, what was affected, the risk to individuals, the measures taken
and the notifications made.  A record may link to the security-ops incident it
came from (``security_incident_id``).

Endpoints (super_admin like the rest of ``/admin/compliance/*``; ``?tenant_id=``
selects another tenant's register):

    GET   /admin/compliance/breaches
    POST  /admin/compliance/breaches            (201)
    GET   /admin/compliance/breaches/{breach_id}
    PATCH /admin/compliance/breaches/{breach_id}

Rules checked on the record as it will be stored (the body on create, the
merged record on update) — 422 ``{user_message, field}`` otherwise:

  * ``occurred_at`` ≤ ``aware_at`` ≤ now, and no notification before
    ``aware_at``;
  * ``delay_reason`` is required when the authority was notified more than
    72 hours after ``aware_at``, or when a breach whose risk is not ``low`` is
    closed without the authority ever being notified (Art. 33(1)).

There is no DELETE — the register is evidence for the supervisory authority.
Every create and update emits an audit event carrying field names and
counts, never the free text.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)
from sqlalchemy import select

from agent_bff.api._audit import emit_admin_audit
from agent_bff.api.v1.admin import require_super_admin_bff
from agent_bff.domain import db as db_module
from agent_bff.domain.models import BreachRecord

router = APIRouter(tags=["Platform Admin"])

# Art. 33(1): notify the supervisory authority within 72 hours of awareness.
NOTIFY_AUTHORITY_WITHIN = timedelta(hours=72)
# Tolerance for "not in the future" checks (browser vs server clocks).
_CLOCK_SKEW = timedelta(minutes=5)
_LIST_LIMIT = 500

Status = Literal["open", "contained", "closed"]
RiskLevel = Literal["low", "medium", "high"]
Title = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
LongText = Annotated[str, StringConstraints(strip_whitespace=True, max_length=10_000)]
Category = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
ShortText = Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)]
IncidentId = Annotated[str, StringConstraints(strip_whitespace=True, max_length=64)]

# Fields that may not be cleared with an explicit null on PATCH.
_REQUIRED_FIELDS = frozenset({
    "title", "description", "aware_at", "status", "risk_level", "data_categories",
    "likely_consequences", "measures_taken",
})
# Optional text fields where an empty string means "not set".
_BLANK_IS_NULL = ("authority_reference", "delay_reason", "security_incident_id")


class BreachCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: Title
    description: LongText = ""
    occurred_at: AwareDatetime | None = None
    aware_at: AwareDatetime
    status: Status = "open"
    risk_level: RiskLevel = "medium"
    data_categories: list[Category] = Field(default_factory=list, max_length=50)
    approx_subjects: int | None = Field(default=None, ge=0)
    approx_records: int | None = Field(default=None, ge=0)
    likely_consequences: LongText = ""
    measures_taken: LongText = ""
    authority_notified_at: AwareDatetime | None = None
    authority_reference: ShortText | None = None
    subjects_notified_at: AwareDatetime | None = None
    delay_reason: LongText | None = None
    security_incident_id: IncidentId | None = None


class BreachPatch(BaseModel):
    """Partial update: omitted fields keep their value, ``null`` clears one."""

    model_config = ConfigDict(extra="forbid")

    title: Title | None = None
    description: LongText | None = None
    occurred_at: AwareDatetime | None = None
    aware_at: AwareDatetime | None = None
    status: Status | None = None
    risk_level: RiskLevel | None = None
    data_categories: list[Category] | None = Field(default=None, max_length=50)
    approx_subjects: int | None = Field(default=None, ge=0)
    approx_records: int | None = Field(default=None, ge=0)
    likely_consequences: LongText | None = None
    measures_taken: LongText | None = None
    authority_notified_at: AwareDatetime | None = None
    authority_reference: ShortText | None = None
    subjects_notified_at: AwareDatetime | None = None
    delay_reason: LongText | None = None
    security_incident_id: IncidentId | None = None

    @model_validator(mode="after")
    def _no_null_for_required(self) -> BreachPatch:
        for name in sorted(self.model_fields_set & _REQUIRED_FIELDS):
            if getattr(self, name) is None:
                raise ValueError(f"{name} can't be cleared")
        return self


# ── helpers ──────────────────────────────────────────────────────────────────

def _invalid(field: str | None, user_message: str) -> HTTPException:
    detail: dict[str, Any] = {"user_message": user_message}
    if field:
        detail["field"] = field
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=detail)


async def _parse(request: Request, model: type[BaseModel]) -> BaseModel:
    """Validate the JSON body; the first problem becomes a readable 422."""
    try:
        raw: Any = await request.json() if await request.body() else {}
    except ValueError:
        raise _invalid(None, "The request body must be valid JSON.") from None
    if not isinstance(raw, dict):
        raise _invalid(None, "The request body must be a JSON object.")
    try:
        return model.model_validate(raw)
    except ValidationError as exc:
        first = exc.errors(include_url=False, include_context=False)[0]
        loc = [str(part) for part in first.get("loc", ()) if not isinstance(part, int)]
        field = loc[0] if loc else None
        message = str(first.get("msg", "invalid value")).removeprefix("Value error, ")
        raise _invalid(field, f"{field}: {message}" if field else message) from None


def _utc(value: datetime | None) -> datetime | None:
    """Normalise to aware UTC (SQLite hands timestamps back naive)."""
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _iso(value: datetime | None) -> str | None:
    normalised = _utc(value)
    return normalised.isoformat() if normalised else None


def _record_of(row: BreachRecord) -> dict[str, Any]:
    """The row's editable fields as a plain dict (timestamps aware UTC)."""
    return {
        "title": row.title,
        "description": row.description,
        "occurred_at": _utc(row.occurred_at),
        "aware_at": _utc(row.aware_at),
        "status": row.status,
        "risk_level": row.risk_level,
        "data_categories": list(row.data_categories or []),
        "approx_subjects": row.approx_subjects,
        "approx_records": row.approx_records,
        "likely_consequences": row.likely_consequences,
        "measures_taken": row.measures_taken,
        "authority_notified_at": _utc(row.authority_notified_at),
        "authority_reference": row.authority_reference,
        "subjects_notified_at": _utc(row.subjects_notified_at),
        "delay_reason": row.delay_reason,
        "security_incident_id": row.security_incident_id,
    }


def _clean(values: dict[str, Any]) -> dict[str, Any]:
    out = dict(values)
    for name in _BLANK_IS_NULL:
        if name in out and out[name] == "":
            out[name] = None
    for name, value in out.items():
        if isinstance(value, datetime):
            out[name] = _utc(value)
    return out


def delay_reason_required(record: dict[str, Any]) -> bool:
    """True when Art. 33(1) requires reasons for a late or missing notification."""
    notified: datetime | None = record.get("authority_notified_at")
    aware: datetime = record["aware_at"]
    if notified is not None:
        return notified - aware > NOTIFY_AUTHORITY_WITHIN
    return bool(record["status"] == "closed" and record["risk_level"] != "low")


def _check(record: dict[str, Any]) -> None:
    latest = datetime.now(UTC) + _CLOCK_SKEW
    aware = record["aware_at"]
    if aware > latest:
        raise _invalid(
            "aware_at", "The time you became aware of the breach can't be in the future.",
        )
    occurred = record.get("occurred_at")
    if occurred is not None and occurred > aware:
        raise _invalid(
            "occurred_at", "The breach can't have happened after you became aware of it.",
        )
    notifications = (
        ("authority_notified_at", "authority"), ("subjects_notified_at", "individuals"),
    )
    for name, who in notifications:
        when = record.get(name)
        if when is None:
            continue
        if when < aware:
            raise _invalid(
                name, f"The {who} can't have been notified before you became aware of the breach.",
            )
        if when > latest:
            raise _invalid(name, f"The {who} notification time can't be in the future.")
    if delay_reason_required(record) and not (record.get("delay_reason") or "").strip():
        if record.get("authority_notified_at") is not None:
            message = (
                "The authority was notified more than 72 hours after you became aware of "
                "the breach. Record the reasons for the delay (GDPR Art. 33(1))."
            )
        else:
            message = (
                "This breach is being closed without notifying the authority. Record why, "
                "or set the risk to low if it is unlikely to affect individuals (GDPR Art. 33(1))."
            )
        raise _invalid("delay_reason", message)


def _serialize(row: BreachRecord) -> dict[str, Any]:
    record = _record_of(row)
    aware = record["aware_at"]
    return {
        "id": row.id,
        "tenant_id": row.tenant_id,
        **{
            name: (value.isoformat() if isinstance(value, datetime) else value)
            for name, value in record.items()
        },
        "notify_authority_by": (aware + NOTIFY_AUTHORITY_WITHIN).isoformat(),
        "created_by": row.created_by,
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }


def _scope(request: Request, auth: tuple[str, str, str]) -> tuple[str, str, str]:
    """(target tenant, admin user id, access token) — ``?tenant_id=`` overrides."""
    caller_tenant, user_id, access_token = auth
    return request.query_params.get("tenant_id") or caller_tenant, user_id, access_token


async def _audit(
    request: Request,
    scope: tuple[str, str, str],
    *,
    event_type: str,
    breach_id: str,
    payload: dict[str, Any],
) -> None:
    tenant_id, user_id, access_token = scope
    await emit_admin_audit(
        request.app.state.settings,
        tenant_id=tenant_id,
        user_id=user_id,
        access_token=access_token,
        event_type=event_type,
        resource_type="breach_record",
        resource_id=breach_id,
        payload=payload,
    )


def _not_found(breach_id: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={"user_message": f"Breach record {breach_id} was not found."},
    )


# ── routes ───────────────────────────────────────────────────────────────────

@router.get("/admin/compliance/breaches")
async def list_breaches(
    request: Request,
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    tenant_id, user_id, _ = _scope(request, _auth)
    async for session in db_module.get_session(
        tenant_id=tenant_id, user_id=user_id, actor="ADMIN",
    ):
        rows = (
            await session.execute(
                select(BreachRecord)
                .where(BreachRecord.tenant_id == tenant_id)
                .order_by(BreachRecord.aware_at.desc(), BreachRecord.id)
                .limit(_LIST_LIMIT)
            )
        ).scalars().all()
        items = [_serialize(row) for row in rows]
    return {"items": items}


@router.post("/admin/compliance/breaches", status_code=201)
async def create_breach(
    request: Request,
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    scope = _scope(request, _auth)
    tenant_id, user_id, _ = scope
    body = await _parse(request, BreachCreate)
    record = _clean(body.model_dump())
    _check(record)
    async for session in db_module.get_session(
        tenant_id=tenant_id, user_id=user_id, actor="ADMIN",
    ):
        row = BreachRecord(tenant_id=tenant_id, created_by=user_id, **record)
        session.add(row)
        await session.flush()
        out = _serialize(row)
    await _audit(
        request, scope,
        event_type="admin.compliance.breach.created",
        breach_id=out["id"],
        payload={
            "status": out["status"],
            "risk_level": out["risk_level"],
            "approx_subjects": out["approx_subjects"],
            "approx_records": out["approx_records"],
            "authority_notified": out["authority_notified_at"] is not None,
        },
    )
    return out


@router.get("/admin/compliance/breaches/{breach_id}")
async def get_breach(
    breach_id: str,
    request: Request,
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    tenant_id, user_id, _ = _scope(request, _auth)
    async for session in db_module.get_session(
        tenant_id=tenant_id, user_id=user_id, actor="ADMIN",
    ):
        row = (
            await session.execute(
                select(BreachRecord).where(
                    BreachRecord.id == breach_id, BreachRecord.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise _not_found(breach_id)
        out = _serialize(row)
    return out


@router.patch("/admin/compliance/breaches/{breach_id}")
async def update_breach(
    breach_id: str,
    request: Request,
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    scope = _scope(request, _auth)
    tenant_id, user_id, _ = scope
    body = await _parse(request, BreachPatch)
    changes = _clean(body.model_dump(exclude_unset=True))
    async for session in db_module.get_session(
        tenant_id=tenant_id, user_id=user_id, actor="ADMIN",
    ):
        row = (
            await session.execute(
                select(BreachRecord).where(
                    BreachRecord.id == breach_id, BreachRecord.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise _not_found(breach_id)
        current = _record_of(row)
        changed = sorted(name for name, value in changes.items() if current[name] != value)
        merged = {**current, **changes}
        _check(merged)
        for name in changed:
            setattr(row, name, changes[name])
        if changed:
            row.updated_at = datetime.now(UTC)
        await session.flush()
        out = _serialize(row)
    if changed:
        await _audit(
            request, scope,
            event_type="admin.compliance.breach.updated",
            breach_id=breach_id,
            payload={
                "changed_fields": changed,
                "status": out["status"],
                "risk_level": out["risk_level"],
            },
        )
    return out
