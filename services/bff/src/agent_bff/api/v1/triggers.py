"""Public inbound-webhook receiver — ``POST /triggers/{id}/webhook``.

Phase 2 of the Trigger/Webhook feature.  An external system fires a webhook
trigger (created in cron-runner, ``trigger_type="webhook"``) by POSTing here with
the per-webhook secret.  Unlike every other BFF route this endpoint is EXEMPT
from user-auth: the BFF's ``SessionMiddleware`` only *populates* identity from a
cookie or the service-token machine path and never rejects, so auth is enforced
per-route by the ``require_auth()`` dependency.  This route simply omits that
dependency and performs its own secret verification instead.

Flow
----
1. Read the presented secret (``Authorization: Bearer <secret>`` or, as a
   fallback, ``X-Webhook-Secret``).
2. Verify it against the trigger's stored hash via cron-runner's internal
   endpoint (the hash never leaves cron-runner) → ``{tenant_id, app_id,
   field_mapping}``.
3. Parse the payload (multipart/form-data or application/json), apply the
   trigger's ``field_mapping`` (incoming key → App input_schema key), and split
   it into text ``inputs`` and file fields — validating against the App's
   published ``input_schema`` so a mistyped field 400s here, not 422 downstream.
4. As the cron service account (machine/service-token path), lazy-create a draft
   session, upload files to it, then re-enter the BFF's own ``/apps/{id}/run``
   resolver with ``source="webhook"``.
5. Return 202 with ``{session_id, run_id}`` for correlation.

URL-mode file references (JSON payloads pointing a file field at a URL / object
key) are intentionally NOT implemented — see ``_parse_json`` for the reasoning.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.datastructures import UploadFile

from agent_bff.api._http import foundation_headers
from agent_bff.api.v1.apps import (
    _USER_ID_RE,
    _raise_for_upstream,
    _select_published_version,
)

router = APIRouter(tags=["Triggers"])
logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Secret extraction + verification
# --------------------------------------------------------------------------- #

def _extract_secret(request: Request) -> str:
    """Read the presented webhook secret; 401 if absent.

    Preferred scheme is ``Authorization: Bearer <secret>``; ``X-Webhook-Secret``
    is accepted as a fallback for callers that cannot set an Authorization
    header.
    """
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        candidate = auth[len("Bearer ") :].strip()
        if candidate:
            return candidate
    fallback = request.headers.get("X-Webhook-Secret", "").strip()
    if fallback:
        return fallback
    raise HTTPException(status_code=401, detail="missing webhook secret")


async def _verify_secret(settings: Any, trigger_id: str, secret: str) -> dict[str, Any]:
    """Exchange the secret for the trigger context via cron-runner (401 on fail).

    The secret hash lives only in cron-runner; the BFF forwards the presented
    secret and gets back ``{tenant_id, app_id, field_mapping, ...}`` on a match.
    Any verify rejection collapses to 401 for the external caller (never leaks
    whether the trigger id exists); a transport failure surfaces as 502.
    """
    url = settings.cron_runner_url.rstrip("/")
    headers = {"X-Service-Token": settings.service_token}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{url}/internal/cron-jobs/{trigger_id}/verify",
                json={"secret": secret},
                headers=headers,
            )
    except httpx.HTTPError as exc:
        logger.warning("webhook verify transport error: %s", exc)
        raise HTTPException(
            status_code=502, detail="trigger verification service unreachable"
        ) from exc

    if resp.status_code != 200:
        # 401/404/anything → single opaque 401 (do not distinguish bad secret
        # from unknown trigger; never echo the secret).
        raise HTTPException(status_code=401, detail="invalid webhook secret or trigger")
    return resp.json()


# --------------------------------------------------------------------------- #
# Fixed (from-config) input values
# --------------------------------------------------------------------------- #

def _flatten_input_values(buckets: Any) -> dict[str, Any]:
    """Flatten a stored ``input_values`` bucket dict into ``{field_key: value}``.

    Mirrors ``cron_runner.scheduler_service`` exactly: the frontend persists
    fixed field values in the SchemaFormValue bucket shape
    ``{"text": {key: str}, "multi": {key: [str]}}`` (no ``file`` bucket — files
    can't be fixed). run_app's ``inputs`` wants a FLAT dict, so text entries and
    multiselect list entries are merged into one dict. A non-dict / empty value
    flattens to ``{}``.
    """
    if not isinstance(buckets, dict):
        return {}
    text = buckets.get("text") if isinstance(buckets.get("text"), dict) else {}
    multi = buckets.get("multi") if isinstance(buckets.get("multi"), dict) else {}
    return {**text, **multi}


# --------------------------------------------------------------------------- #
# Body-size guard
# --------------------------------------------------------------------------- #

def _enforce_body_size(request: Request, max_bytes: int) -> None:
    """Reject oversized payloads up-front via Content-Length (413)."""
    raw = request.headers.get("content-length")
    if raw:
        try:
            length = int(raw)
        except ValueError:
            return
        if length > max_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"payload too large ({length} bytes > {max_bytes})",
            )


# --------------------------------------------------------------------------- #
# Payload parsing → (inputs, pending_files)
# --------------------------------------------------------------------------- #

def _coerce_text(values: list[str]) -> Any:
    """Coerce raw form value(s) for a non-file field.

    A repeated key yields a list; a single value is kept as its string form
    unless it is a JSON-encoded array (so multiselect fields can arrive either
    as repeated keys or as one ``["a","b"]`` value).  Scalars are deliberately
    NOT JSON-coerced — ``"123"`` stays the string ``"123"`` so a select's option
    matching is not silently broken by turning it into an int.
    """
    if len(values) > 1:
        return values
    v = values[0]
    try:
        parsed = json.loads(v)
    except (ValueError, TypeError):
        return v
    return parsed if isinstance(parsed, list) else v


async def _parse_multipart(
    request: Request,
    field_mapping: dict[str, str],
    schema_by_key: dict[str, dict],
    file_keys: set[str],
) -> tuple[dict[str, Any], dict[str, list[tuple[str, bytes, str]]], list[str]]:
    """Parse multipart/form-data into (inputs, pending_files, errors)."""
    form = await request.form()
    errors: list[str] = []
    text_by_key: dict[str, list[str]] = {}
    pending_files: dict[str, list[tuple[str, bytes, str]]] = {}

    for incoming_key, value in form.multi_items():
        app_key = field_mapping.get(incoming_key, incoming_key)
        if app_key not in schema_by_key:
            errors.append(f"unknown field '{incoming_key}' (maps to '{app_key}')")
            continue
        is_file_field = app_key in file_keys
        if isinstance(value, UploadFile):
            if not is_file_field:
                errors.append(
                    f"field '{incoming_key}' is a file but App field "
                    f"'{app_key}' is not a file input"
                )
                continue
            content = await value.read()
            pending_files.setdefault(app_key, []).append(
                (
                    value.filename or "upload.bin",
                    content,
                    value.content_type or "application/octet-stream",
                )
            )
        else:
            if is_file_field:
                errors.append(
                    f"field '{incoming_key}' is a text value but App field "
                    f"'{app_key}' expects a file"
                )
                continue
            text_by_key.setdefault(app_key, []).append(value)

    inputs = {k: _coerce_text(v) for k, v in text_by_key.items()}
    return inputs, pending_files, errors


async def _parse_json(
    request: Request,
    field_mapping: dict[str, str],
    schema_by_key: dict[str, dict],
    file_keys: set[str],
) -> tuple[dict[str, Any], dict[str, list[tuple[str, bytes, str]]], list[str]]:
    """Parse application/json into (inputs, {}, errors).

    JSON payloads carry no bytes, so a file field in JSON mode would have to
    reference a URL or object key that the BFF then fetches server-side.  That
    URL-mode is NOT implemented: the only server-side fetch path is
    web-fetch-gateway, which (a) requires a scheduler-issued bearer + the
    ``web_fetch.invoke`` service scope the BFF does not hold, and (b) returns
    text-only bodies (``restrict_content_types``), so binary attachments would
    be rejected/corrupted.  Rather than add an unsafe direct fetch, a file field
    in JSON mode is a 400 that tells the caller to use multipart/form-data.
    """
    try:
        data = await request.json()
    except Exception as exc:  # noqa: BLE001 - malformed JSON → 400
        return {}, {}, [f"invalid JSON body: {exc}"]
    if not isinstance(data, dict):
        return {}, {}, ["JSON body must be an object"]

    errors: list[str] = []
    inputs: dict[str, Any] = {}
    for incoming_key, value in data.items():
        app_key = field_mapping.get(incoming_key, incoming_key)
        if app_key not in schema_by_key:
            errors.append(f"unknown field '{incoming_key}' (maps to '{app_key}')")
            continue
        if app_key in file_keys:
            errors.append(
                f"file field '{app_key}' cannot be supplied via JSON "
                "(URL / object-key references are not supported yet — "
                "send the file with multipart/form-data instead)"
            )
            continue
        inputs[app_key] = value
    return inputs, {}, errors


# --------------------------------------------------------------------------- #
# Route
# --------------------------------------------------------------------------- #

@router.post("/triggers/{trigger_id}/webhook", status_code=202)
async def receive_webhook(trigger_id: str, request: Request) -> JSONResponse:
    """Public webhook entrypoint — verify secret, then launch an App run.

    Returns 202 ``{session_id, run_id}``.  Auth is the per-webhook secret only;
    this route is deliberately exempt from ``require_auth``.
    """
    settings = request.app.state.settings

    secret = _extract_secret(request)
    _enforce_body_size(request, settings.webhook_max_body_bytes)
    verified = await _verify_secret(settings, trigger_id, secret)

    tenant_id: str = verified["tenant_id"]
    app_id: str = verified["app_id"]
    field_mapping: dict[str, str] = verified.get("field_mapping") or {}
    # Fixed (from-config) inputs stored on the trigger, flattened to a flat
    # ``{field_key: value}`` dict. These SEED the run inputs; payload-parsed
    # inputs are overlaid on top below (the payload wins any overlapping key).
    # field_mapping still only covers from-event keys — fixed fields arrive here,
    # not in the payload.
    fixed_inputs: dict[str, Any] = _flatten_input_values(verified.get("input_values"))
    # The trigger's owner: the run executes as the cron service account, so
    # the session records whose behalf it is on for that user's export and
    # erasure (NULL for triggers created before cron-runner tracked owners).
    owner = verified.get("created_by_user_id")
    if not (isinstance(owner, str) and _USER_ID_RE.fullmatch(owner)):
        owner = None

    # Machine identity for foundation calls (no bearer — foundation services
    # trust X-Tenant-Id / X-User-Id on the internal hop) and for the
    # service-token self-call into /apps/{id}/run.
    cron_user = settings.cron_service_user_id
    fheaders = foundation_headers(tenant_id, cron_user, "")
    machine_headers = {
        "X-Service-Token": settings.service_token,
        "X-Tenant-Id": tenant_id,
        "X-User-Id": cron_user,
    }

    registry_url = settings.app_registry_url
    orch_url = settings.session_orchestrator_url
    self_url = settings.bff_self_url.rstrip("/")
    api_prefix = settings.api_prefix

    async with httpx.AsyncClient(timeout=30) as client:
        # 1. Resolve the App's published version → input_schema + version id.
        app_resp = await client.get(f"{registry_url}/apps/{app_id}", headers=fheaders)
        _raise_for_upstream(app_resp)
        app = app_resp.json()
        version = _select_published_version(app)
        vid = version["id"]
        input_schema: list[dict] = version.get("input_schema") or []
        schema_by_key = {f["key"]: f for f in input_schema}
        file_keys = {
            f["key"] for f in input_schema if f.get("type") == "file"
        }

        # 2. Parse + map + type-validate the payload.
        content_type = request.headers.get("content-type", "")
        # Both multipart (files) and urlencoded (text-only) forms parse via
        # request.form(); urlencoded simply never yields UploadFile parts.
        if content_type.startswith(
            ("multipart/form-data", "application/x-www-form-urlencoded")
        ):
            inputs, pending_files, errors = await _parse_multipart(
                request, field_mapping, schema_by_key, file_keys
            )
        elif content_type.startswith("application/json"):
            inputs, pending_files, errors = await _parse_json(
                request, field_mapping, schema_by_key, file_keys
            )
        else:
            raise HTTPException(
                status_code=415,
                detail=(
                    "unsupported content-type; use multipart/form-data or "
                    "application/json"
                ),
            )
        if errors:
            raise HTTPException(
                status_code=400,
                detail={"error": "invalid_webhook_payload", "errors": errors},
            )

        # 3. If the payload carried files, lazy-create a draft session and
        #    upload them (attachments are session-scoped), collecting their ids
        #    into file_inputs keyed by App field.
        session_id: str | None = None
        file_inputs: dict[str, list[str]] = {}
        if pending_files:
            agent_config: dict[str, Any] = {
                "kind": "app_run",
                "app_id": app_id,
                "app_version_id": vid,
            }
            if owner:
                # run_app tags only the sessions it creates itself.
                agent_config["on_behalf_of_user_id"] = owner
            sess_resp = await client.post(
                f"{orch_url}/sessions",
                json={
                    "title": app.get("name") or "Webhook run",
                    "agent_config": agent_config,
                },
                headers=fheaders,
            )
            _raise_for_upstream(sess_resp)
            session_id = sess_resp.json()["id"]
            for app_key, files in pending_files.items():
                ids: list[str] = []
                for filename, content, mime in files:
                    up_resp = await client.post(
                        f"{orch_url}/sessions/{session_id}/attachments",
                        files={"file": (filename, content, mime)},
                        headers=fheaders,
                    )
                    _raise_for_upstream(up_resp)
                    ids.append(up_resp.json()["id"])
                file_inputs[app_key] = ids

        # 4. Merge fixed + from-event inputs (payload wins any overlapping key),
        #    then re-enter the BFF's own run resolver over HTTP (machine path) so
        #    all validation / rendering / snapshot / scheduler-relay stays in one
        #    place.  source="webhook" forces sandbox execution downstream.
        merged_inputs: dict[str, Any] = {**fixed_inputs, **inputs}
        run_body: dict[str, Any] = {
            "inputs": merged_inputs,
            "file_inputs": file_inputs,
            "source": "webhook",
        }
        if session_id:
            run_body["session_id"] = session_id
        if owner:
            run_body["on_behalf_of"] = owner
        run_resp = await client.post(
            f"{self_url}{api_prefix}/apps/{app_id}/run",
            json=run_body,
            headers=machine_headers,
        )
        _raise_for_upstream(run_resp)
        run = run_resp.json()

    return JSONResponse(
        status_code=202,
        content={
            "session_id": run.get("session_id") or session_id,
            "run_id": run.get("run_id"),
        },
    )
