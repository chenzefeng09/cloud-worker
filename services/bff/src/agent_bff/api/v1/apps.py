"""App proxy endpoints + the "run an App" resolver.

CRUD / version / submit / publish / review endpoints are pure pass-throughs to
the ``app-registry`` service (mirrors ``library.py`` style: forward the caller's
foundation headers, relay status/body via ``_raise_for_upstream``).

The interesting endpoint is ``POST /apps/{id}/run`` — it resolves the published
harness for an App, validates + renders the caller's inputs against the version's
``input_schema``, creates (or reuses) a session, builds an immutable
``app_snapshot``, and triggers a single run on the orchestrator carrying the
fixed harness (``sys_prompt`` / ``sampling_params`` / forced skills / model).

Attachment-scoping contract for ``file_inputs`` (option A):
    Attachments are session-scoped in this codebase (uploaded via
    ``POST /sessions/{id}/attachments`` and only resolvable within that
    session).  The frontend therefore lazy-creates a draft session BEFORE the
    run, uploads files to it, and passes that ``session_id`` in the run body.
    The run resolver reuses that session instead of creating a new one.  If no
    ``session_id`` is supplied (an App with no file inputs) the resolver creates
    a fresh session itself.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from agent_bff.api._http import foundation_headers
from agent_bff.api.dependencies import require_auth
from agent_bff.api.v1.conversations import _resolve_attachment_files

router = APIRouter(tags=["Apps"])
logger = logging.getLogger(__name__)

# Each rendered text value embedded in the snapshot is truncated to this many
# bytes — the full text lives in task.json / attachments, the snapshot is only a
# human-readable record of "what was filled in".
_SNAPSHOT_VALUE_MAX = 4096
_PARAM_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9_])/([a-z][a-z0-9_]{0,39})(?![A-Za-z0-9_])")


def _raise_for_upstream(resp: httpx.Response) -> None:
    """Surface upstream 4xx/5xx as HTTPException so the SPA gets a readable error."""
    if resp.status_code >= 400:
        try:
            detail = resp.json()
        except Exception:
            detail = resp.text
        raise HTTPException(status_code=resp.status_code, detail=detail)


def _registry_url(request: Request) -> str:
    return request.app.state.settings.app_registry_url


def _orchestrator_url(request: Request) -> str:
    return request.app.state.settings.session_orchestrator_url


# --------------------------------------------------------------------------- #
# Pure proxy endpoints (mirror library.py)
# --------------------------------------------------------------------------- #


@router.get("/apps")
async def list_apps(
    request: Request,
    scope: str | None = None,
    _auth: tuple = Depends(require_auth()),
) -> JSONResponse:
    """List visible Apps (``?scope=mine|published``; app-registry defaults to published)."""
    tenant_id, user_id, access_token = _auth
    url = _registry_url(request)
    params: dict[str, str] = {}
    if scope:
        params["scope"] = scope
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{url}/apps",
            params=params,
            headers=foundation_headers(tenant_id, user_id, access_token),
        )
    _raise_for_upstream(resp)
    return JSONResponse(content=resp.json(), status_code=resp.status_code)


@router.post("/apps", status_code=201)
async def create_app(
    request: Request,
    _auth: tuple = Depends(require_auth()),
) -> JSONResponse:
    """Create an App + its initial draft version in app-registry."""
    tenant_id, user_id, access_token = _auth
    url = _registry_url(request)
    body = await request.json()
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{url}/apps",
            json=body,
            headers=foundation_headers(tenant_id, user_id, access_token),
        )
    _raise_for_upstream(resp)
    return JSONResponse(content=resp.json(), status_code=resp.status_code)


@router.get("/apps/review/pending")
async def review_pending(
    request: Request,
    _auth: tuple = Depends(require_auth()),
) -> JSONResponse:
    """Admin review queue: pending_review versions + their App context."""
    tenant_id, user_id, access_token = _auth
    url = _registry_url(request)
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{url}/apps/review/pending",
            headers=foundation_headers(tenant_id, user_id, access_token),
        )
    _raise_for_upstream(resp)
    return JSONResponse(content=resp.json(), status_code=resp.status_code)


@router.post("/apps/review/{vid}/approve")
async def review_approve(
    vid: str,
    request: Request,
    _auth: tuple = Depends(require_auth()),
) -> JSONResponse:
    """Approve a pending version (publish it)."""
    tenant_id, user_id, access_token = _auth
    url = _registry_url(request)
    try:
        body = await request.json()
    except Exception:
        body = {}
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{url}/apps/review/{vid}/approve",
            json=body or {},
            headers=foundation_headers(tenant_id, user_id, access_token),
        )
    _raise_for_upstream(resp)
    return JSONResponse(content=resp.json(), status_code=resp.status_code)


@router.post("/apps/review/{vid}/reject")
async def review_reject(
    vid: str,
    request: Request,
    _auth: tuple = Depends(require_auth()),
) -> JSONResponse:
    """Reject a pending version (note required by the downstream service)."""
    tenant_id, user_id, access_token = _auth
    url = _registry_url(request)
    try:
        body = await request.json()
    except Exception:
        body = {}
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{url}/apps/review/{vid}/reject",
            json=body or {},
            headers=foundation_headers(tenant_id, user_id, access_token),
        )
    _raise_for_upstream(resp)
    return JSONResponse(content=resp.json(), status_code=resp.status_code)


@router.get("/apps/{app_id}")
async def get_app(
    app_id: str,
    request: Request,
    _auth: tuple = Depends(require_auth()),
) -> JSONResponse:
    """Fetch an App + its versions (author sees all; others see published current)."""
    tenant_id, user_id, access_token = _auth
    url = _registry_url(request)
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{url}/apps/{app_id}",
            headers=foundation_headers(tenant_id, user_id, access_token),
        )
    _raise_for_upstream(resp)
    return JSONResponse(content=resp.json(), status_code=resp.status_code)


@router.put("/apps/{app_id}")
async def update_app(
    app_id: str,
    request: Request,
    _auth: tuple = Depends(require_auth()),
) -> JSONResponse:
    """Update App-level fields (name/description/icon/visibility)."""
    tenant_id, user_id, access_token = _auth
    url = _registry_url(request)
    body = await request.json()
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.put(
            f"{url}/apps/{app_id}",
            json=body,
            headers=foundation_headers(tenant_id, user_id, access_token),
        )
    _raise_for_upstream(resp)
    return JSONResponse(content=resp.json(), status_code=resp.status_code)


@router.delete("/apps/{app_id}", status_code=204)
async def delete_app(
    app_id: str,
    request: Request,
    _auth: tuple = Depends(require_auth()),
) -> Response:
    """Delete an App (author or admin)."""
    tenant_id, user_id, access_token = _auth
    url = _registry_url(request)
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.delete(
            f"{url}/apps/{app_id}",
            headers=foundation_headers(tenant_id, user_id, access_token),
        )
    _raise_for_upstream(resp)
    return Response(status_code=204)


@router.post("/apps/{app_id}/versions", status_code=201)
async def create_version(
    app_id: str,
    request: Request,
    _auth: tuple = Depends(require_auth()),
) -> JSONResponse:
    """Create a new draft version on an App."""
    tenant_id, user_id, access_token = _auth
    url = _registry_url(request)
    body = await request.json()
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{url}/apps/{app_id}/versions",
            json=body,
            headers=foundation_headers(tenant_id, user_id, access_token),
        )
    _raise_for_upstream(resp)
    return JSONResponse(content=resp.json(), status_code=resp.status_code)


@router.put("/apps/{app_id}/versions/{vid}")
async def update_version(
    app_id: str,
    vid: str,
    request: Request,
    _auth: tuple = Depends(require_auth()),
) -> JSONResponse:
    """Edit a draft version (non-draft is rejected downstream)."""
    tenant_id, user_id, access_token = _auth
    url = _registry_url(request)
    body = await request.json()
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.put(
            f"{url}/apps/{app_id}/versions/{vid}",
            json=body,
            headers=foundation_headers(tenant_id, user_id, access_token),
        )
    _raise_for_upstream(resp)
    return JSONResponse(content=resp.json(), status_code=resp.status_code)


@router.post("/apps/{app_id}/versions/{vid}/submit")
async def submit_version(
    app_id: str,
    vid: str,
    request: Request,
    _auth: tuple = Depends(require_auth()),
) -> JSONResponse:
    """Submit a draft version for review (draft → pending_review)."""
    tenant_id, user_id, access_token = _auth
    url = _registry_url(request)
    try:
        body = await request.json()
    except Exception:
        body = {}
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{url}/apps/{app_id}/versions/{vid}/submit",
            json=body or {},
            headers=foundation_headers(tenant_id, user_id, access_token),
        )
    _raise_for_upstream(resp)
    return JSONResponse(content=resp.json(), status_code=resp.status_code)


@router.post("/apps/{app_id}/versions/{vid}/publish")
async def publish_version(
    app_id: str,
    vid: str,
    request: Request,
    _auth: tuple = Depends(require_auth()),
) -> JSONResponse:
    """Publish a version directly (admin author shortcut)."""
    tenant_id, user_id, access_token = _auth
    url = _registry_url(request)
    try:
        body = await request.json()
    except Exception:
        body = {}
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{url}/apps/{app_id}/versions/{vid}/publish",
            json=body or {},
            headers=foundation_headers(tenant_id, user_id, access_token),
        )
    _raise_for_upstream(resp)
    return JSONResponse(content=resp.json(), status_code=resp.status_code)


# --------------------------------------------------------------------------- #
# Run resolver — POST /apps/{id}/run
# --------------------------------------------------------------------------- #


def _select_published_version(app: dict) -> dict:
    """Pick the published harness version from an AppOut payload.

    Prefers ``current_version_id`` (set by the publish/approve flow).  Raises
    409 if the App has no published version.
    """
    versions: list[dict] = app.get("versions") or []
    current_vid = app.get("current_version_id")
    if current_vid:
        for v in versions:
            if v.get("id") == current_vid:
                if v.get("status") == "published":
                    return v
                break
    # Fallback: any published version (defensive — current_version_id should
    # always point at a published one).
    published = [v for v in versions if v.get("status") == "published"]
    if published:
        return published[0]
    raise HTTPException(
        status_code=409,
        detail={"error": "app_not_published", "message": "This App has no published version to run."},
    )


def _truncate(value: Any) -> Any:
    """Truncate long string values for the snapshot (full text lives in task.json)."""
    if isinstance(value, str) and len(value.encode("utf-8")) > _SNAPSHOT_VALUE_MAX:
        return value.encode("utf-8")[:_SNAPSHOT_VALUE_MAX].decode("utf-8", "ignore") + "…"
    return value


def _sandbox_input_path(filename: str | None) -> str:
    """Return the stable path where runtime_worker downloads an input file."""
    return f"/workspace/inputs/{filename or 'file'}"


def _validate_and_collect(
    *,
    input_schema: list[dict],
    inputs: dict[str, Any],
    file_inputs: dict[str, list[str]],
    attachments_by_id: dict[str, dict],
) -> tuple[dict[str, Any], list[str]]:
    """Validate inputs/file_inputs against the schema.

    Returns ``(resolved_values, errors)`` where ``resolved_values`` maps each
    field key to a render-ready value:
      * text/textarea/select → the string value
      * multiselect          → list of selected values
      * file                 → list of ``{attachment_id, filename, mime}``
    ``errors`` is a list of human-readable problems (empty == valid).
    """
    errors: list[str] = []
    resolved: dict[str, Any] = {}

    known_keys = {f["key"] for f in input_schema}

    # Reject unknown keys.
    for k in inputs:
        if k not in known_keys:
            errors.append(f"unknown input field '{k}'")
    for k in file_inputs:
        if k not in known_keys:
            errors.append(f"unknown file input field '{k}'")

    for field in input_schema:
        key = field["key"]
        ftype = field["type"]
        required = bool(field.get("required"))
        label = field.get("label") or key

        if ftype == "file":
            ids = file_inputs.get(key) or []
            if not ids:
                default_raw = field.get("default")
                default_paths = (
                    default_raw if isinstance(default_raw, list)
                    else ([default_raw] if isinstance(default_raw, str) and default_raw else [])
                )
                if default_paths:
                    resolved[key] = [
                        {
                            "attachment_id": None,
                            "filename": str(path).rstrip("/").split("/")[-1] or "file",
                            "mime": "application/octet-stream",
                            "path": str(path),
                        }
                        for path in default_paths
                    ]
                    continue
                if required:
                    errors.append(f"'{label}' is required")
                resolved[key] = []
                continue
            fc = field.get("file") or {}
            accept = fc.get("accept") or []
            max_size = fc.get("max_size_bytes")
            multiple = bool(fc.get("multiple"))
            if not multiple and len(ids) > 1:
                errors.append(f"'{label}' accepts only one file")
            file_vals: list[dict] = []
            for aid in ids:
                att = attachments_by_id.get(aid)
                if att is None:
                    errors.append(f"'{label}': attachment '{aid}' not found in session")
                    continue
                mime = att.get("mime") or "application/octet-stream"
                size = att.get("size_bytes")
                if accept and mime not in accept:
                    errors.append(
                        f"'{label}': file type '{mime}' not allowed (accepts {accept})"
                    )
                if max_size is not None and size is not None and size > max_size:
                    errors.append(
                        f"'{label}': file '{att.get('filename')}' exceeds max size {max_size} bytes"
                    )
                file_vals.append({
                    "attachment_id": aid,
                    "filename": att.get("filename"),
                    "mime": mime,
                    "path": _sandbox_input_path(att.get("filename")),
                })
            resolved[key] = file_vals
            continue

        # Non-file fields
        raw = inputs.get(key, field.get("default"))
        if ftype == "multiselect":
            vals = raw if isinstance(raw, list) else ([] if raw in (None, "") else [raw])
            allowed = {o["value"] for o in (field.get("options") or [])}
            bad = [v for v in vals if v not in allowed]
            if bad:
                errors.append(f"'{label}': invalid option(s) {bad}")
            if required and not vals:
                errors.append(f"'{label}' is required")
            resolved[key] = vals
            continue

        # text / textarea / select
        val = "" if raw is None else str(raw)
        if ftype == "select" and val:
            allowed = {o["value"] for o in (field.get("options") or [])}
            if val not in allowed:
                errors.append(f"'{label}': '{val}' is not a valid option")
        if required and val == "":
            errors.append(f"'{label}' is required")
        resolved[key] = val

    return resolved, errors


def _render_template(template: str, input_schema: list[dict], resolved: dict[str, Any]) -> str:
    """Render parameter references in a fixed App prompt.

    Preferred syntax is ``/key`` so authors can insert parameters quickly from
    the editor. The legacy ``{{key}}`` form is still supported for existing
    drafts/tests.

    Only known keys are substituted.
    * multiselect → comma-joined selected values
    * file        → comma-joined sandbox file paths
    * missing/optional → empty string
    """
    rendered_by_key: dict[str, str] = {}
    for field in input_schema:
        key = field["key"]
        ftype = field["type"]
        value = resolved.get(key)
        if ftype == "file":
            rendered = ", ".join(v.get("path") or "" for v in (value or []))
        elif ftype == "multiselect":
            rendered = ", ".join(value or [])
        else:
            rendered = "" if value is None else str(value)
        rendered_by_key[key] = rendered

    out = template
    for key, rendered in rendered_by_key.items():
        out = out.replace("{{" + key + "}}", rendered)

    def _replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in rendered_by_key:
            return match.group(0)
        return rendered_by_key[key]

    out = _PARAM_TOKEN_RE.sub(_replace, out)
    return out


@router.post("/apps/{app_id}/run")
async def run_app(
    app_id: str,
    request: Request,
    _auth: tuple = Depends(require_auth()),
) -> JSONResponse:
    """Resolve an App's published harness, validate+render inputs, trigger a run.

    Body::

        {
          "inputs": {field_key: value, ...},
          "file_inputs": {field_key: [attachment_id, ...], ...},
          "session_id": "sess_..."   // optional: a draft session the frontend
                                     //   already created + uploaded files to
        }

    Returns ``{session_id, run_id, status}``.
    """
    tenant_id, user_id, access_token = _auth
    registry_url = _registry_url(request)
    orch_url = _orchestrator_url(request)
    headers = foundation_headers(tenant_id, user_id, access_token)

    body = await request.json() if await request.body() else {}
    inputs: dict[str, Any] = body.get("inputs") or {}
    file_inputs: dict[str, list[str]] = body.get("file_inputs") or {}
    session_id: str | None = body.get("session_id") or None

    async with httpx.AsyncClient(timeout=30) as client:
        # 1. Fetch the App + versions (RLS + visibility authorize the caller).
        resp = await client.get(f"{registry_url}/apps/{app_id}", headers=headers)
        _raise_for_upstream(resp)
        app = resp.json()

        # 2. Select the published harness version.
        version = _select_published_version(app)
        vid = version["id"]
        system_prompt = version["system_prompt"]
        user_prompt_template = version["user_prompt_template"]
        model = version.get("model")
        reasoning_effort = version.get("reasoning_effort")
        sampling_params = version.get("sampling_params") or {}
        forced_skills = version.get("forced_skills") or []
        forced_mcp_server_ids = version.get("forced_mcp_server_ids") or []
        input_schema: list[dict] = version.get("input_schema") or []
        app_version = version.get("version")

        # 3. Resolve attachment metadata (if any file_inputs) against the
        #    session the frontend uploaded them to.  Attachments are
        #    session-scoped, so file_inputs require a session_id.
        attachments_by_id: dict[str, dict] = {}
        any_file_ids = any(file_inputs.get(k) for k in file_inputs)
        if any_file_ids:
            if not session_id:
                raise HTTPException(
                    status_code=422,
                    detail={
                        "error": "session_required",
                        "message": "file_inputs require a session_id the files were uploaded to",
                    },
                )
            att_resp = await client.get(
                f"{orch_url}/sessions/{session_id}/attachments",
                headers=headers,
            )
            if att_resp.status_code == 200:
                for a in att_resp.json().get("items", []):
                    attachments_by_id[a["id"]] = a

        # 4. Validate inputs against the schema.
        resolved, errors = _validate_and_collect(
            input_schema=input_schema,
            inputs=inputs,
            file_inputs=file_inputs,
            attachments_by_id=attachments_by_id,
        )
        if errors:
            raise HTTPException(
                status_code=422,
                detail={"error": "invalid_inputs", "errors": errors},
            )

        # 5. Render the user prompt template → task text.
        task_text = _render_template(user_prompt_template, input_schema, resolved)

        # 6. Create the session (or reuse the frontend's draft session).
        #    NOTE: the orchestrator's SessionCreate carries metadata via the
        #    `agent_config` body field (mapped to the ORM `agent_metadata`
        #    column).  Its SessionUpdate does NOT accept metadata, so the
        #    app_run tag can only be set at create time — when the frontend
        #    lazy-creates the draft session for file uploads it must pass
        #    `agent_config={"kind":"app_run", ...}` itself.  The single-turn
        #    lock in the result page is the primary guard; this tag is the
        #    documented double-insurance per plan §2.
        app_run_meta = {
            "kind": "app_run",
            "app_id": app_id,
            "app_version_id": vid,
        }
        if not session_id:
            sess_resp = await client.post(
                f"{orch_url}/sessions",
                json={
                    "title": app.get("name") or "App run",
                    "agent_config": app_run_meta,
                },
                headers=headers,
            )
            _raise_for_upstream(sess_resp)
            session_id = sess_resp.json()["id"]

        # 7. Resolve the flat list of file attachment ids → worker file specs.
        all_attachment_ids = [
            aid for k in file_inputs for aid in (file_inputs.get(k) or [])
        ]
        files = await _resolve_attachment_files(
            client=client,
            orch_url=orch_url,
            session_id=session_id,
            attachment_ids=all_attachment_ids,
            headers=headers,
        )

        # 8. Build the immutable app_snapshot (plan §4).
        snapshot_inputs: list[dict] = []
        for field in input_schema:
            key = field["key"]
            ftype = field["type"]
            value = resolved.get(key)
            snapshot_inputs.append({
                "key": key,
                "label": field.get("label") or key,
                "type": ftype,
                "value": value if ftype in ("file", "multiselect") else _truncate(value),
            })
        app_snapshot = {
            "app_id": app_id,
            "app_name": app.get("name"),
            "app_version": app_version,
            "app_version_id": vid,
            "model": model,
            "reasoning_effort": reasoning_effort,
            "sampling_params": sampling_params,
            "forced_skills": forced_skills,
            "forced_mcp_server_ids": forced_mcp_server_ids,
            "system_prompt": system_prompt,
            "user_prompt_template": user_prompt_template,
            "rendered_task_preview": _truncate(task_text),
            "inputs": snapshot_inputs,
            "snapshot_at": datetime.now(UTC).isoformat(),
        }

        # 9. Trigger the run carrying the fixed harness.
        run_resp = await client.post(
            f"{orch_url}/sessions/{session_id}/runs",
            json={
                "task": task_text,
                "files": files,
                "skills": forced_skills,
                "model": model,
                "reasoning_effort": reasoning_effort,
                "sys_prompt": system_prompt,
                "sampling_params": sampling_params,
                "app_snapshot": app_snapshot,
            },
            headers=headers,
        )
        _raise_for_upstream(run_resp)
        run = run_resp.json()

    return JSONResponse(
        content={
            "session_id": session_id,
            "run_id": run.get("id"),
            "status": run.get("status"),
        },
        status_code=201,
    )
