"""Artifacts panel BFF endpoint — merge a user's canvases + output files.

A single ``GET /artifacts`` fans out in parallel to two orchestrator endpoints
(``/artifacts/canvases`` and ``/artifacts/files?kind=output``) and merges the
results into one timeline of artifact dicts, sorted by ``created_at`` desc.
The frontend renders this as one unified "Artifacts 专区" grid; delete is a
per-item call to the existing canvas / attachment DELETE proxies, not here.

Fan-out uses one shared ``httpx.AsyncClient`` (one connection pool) with
``asyncio.gather`` so the two calls overlap.  Failure posture matches the
other BFF proxies: an orchestrator non-2xx is forwarded as-is via
``_proxy_json``-style status passthrough; an unreachable orchestrator returns
502 with an English ``user_message``.

Auth: ``require_auth()`` — the BFF re-resolves ``(tenant_id, user_id,
access_token)`` from the cookie session and forwards them as
``foundation_headers``.  Owner-scoping happens in the orchestrator (the
``user_id`` filter in the service); the BFF never widens scope.
"""

from __future__ import annotations

import asyncio
import logging

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from agent_bff.api._http import foundation_headers
from agent_bff.api.dependencies import require_auth

router = APIRouter(tags=["Artifacts"])
logger = logging.getLogger(__name__)

_TIMEOUT = 15


def _iso(value: object) -> str | None:
    """Coerce a possibly-missing datetime-ish value to a sortable ISO string."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    # Pydantic / datetime objects expose ``isoformat()``.
    iso = getattr(value, "isoformat", None)
    if callable(iso):
        try:
            return iso()
        except Exception:  # noqa: BLE001
            return None
    return str(value)


async def _fetch(
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    kind: str,
) -> tuple[str, httpx.Response]:
    return kind, await client.get(url, headers=headers)


@router.get("/artifacts")
async def list_artifacts(
    request: Request,
    _auth: tuple = Depends(require_auth()),
) -> JSONResponse:
    """Merge the caller's canvases + output files into one artifacts timeline.

    Response::

        {"items": [
            {"id", "kind": "canvas"|"file",
             "title", "filename?", "mime?", "size_bytes?",
             "session_id", "run_id?", "canvas_kind?", "current_version?",
             "created_at", "updated_at?"},
            ...
        ]}

    Sorted by ``created_at`` desc.  Canvas ``id`` (``cvs_…``) and file ``id``
    (``att_…``) live in separate namespaces so the ``kind`` discriminator is the
    authoritative router key on the frontend.
    """
    tenant_id, user_id, access_token = _auth
    orch_url = request.app.state.settings.session_orchestrator_url.rstrip("/")
    headers = foundation_headers(tenant_id, user_id, access_token)

    canvas_url = f"{orch_url}/artifacts/canvases"
    files_url = f"{orch_url}/artifacts/files"
    params = {"kind": "output"}

    canvases: list[dict] = []
    files: list[dict] = []
    upstream_status = 200

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            results = await asyncio.gather(
                _fetch(client, canvas_url, headers, "canvas"),
                _fetch(client, files_url, headers, "file"),
                return_exceptions=True,
            )
    except httpx.HTTPError as exc:
        logger.warning("artifacts fan-out: orchestrator unreachable: %s", exc)
        return JSONResponse(
            status_code=502,
            content={
                "detail": "The artifacts service is temporarily unavailable. Please try again later.",
                "user_message": "The artifacts service is temporarily unavailable. Please try again later.",
                "error": str(exc),
            },
        )

    for result in results:
        if isinstance(result, Exception):
            # One leg failed (timeout / connect error) — best-effort: treat as
            # empty rather than failing the whole merge.  Logged so the operator
            # can see a partial outage.
            logger.warning("artifacts fan-out leg failed: %s", result)
            continue
        kind, resp = result
        if resp.status_code >= 400:
            # Forward the worst upstream status; if one leg errored, surface it
            # rather than silently swallowing.  We still merge whatever the
            # other leg returned.
            upstream_status = max(upstream_status, resp.status_code)
            logger.warning(
                "artifacts fan-out %s leg: upstream %s",
                kind, resp.status_code,
            )
            continue
        try:
            items = (resp.json() or {}).get("items") or []
        except Exception:  # noqa: BLE001
            logger.warning("artifacts fan-out %s leg: non-JSON body", kind)
            continue
        if kind == "canvas":
            canvases = items
        else:
            files = items

    # ── merge into the unified artifact shape ──────────────────────────────
    merged: list[dict] = []
    for c in canvases:
        merged.append(
            {
                "id": c.get("id"),
                "kind": "canvas",
                "title": c.get("title"),
                "session_id": c.get("session_id"),
                "run_id": c.get("source_run_id"),
                "canvas_kind": c.get("kind"),
                "current_version": c.get("current_version"),
                "created_at": _iso(c.get("created_at")),
                "updated_at": _iso(c.get("updated_at")),
            }
        )
    for f in files:
        merged.append(
            {
                "id": f.get("id"),
                "kind": "file",
                "title": f.get("filename"),
                "filename": f.get("filename"),
                "mime": f.get("mime"),
                "size_bytes": f.get("size_bytes"),
                "session_id": f.get("session_id"),
                "run_id": f.get("run_id"),
                "created_at": _iso(f.get("created_at")),
            }
        )

    # Sort by created_at desc; None sorts last (treat missing as empty string
    # so a missing timestamp never crashes the comparator).
    merged.sort(
        key=lambda a: a.get("created_at") or "",
        reverse=True,
    )

    # When both legs succeeded we return 200; if either leg errored we surface
    # the upstream status (still with whatever partial items we merged) so the
    # SPA can render a partial state without a hard failure masking the cause.
    status_code = 200 if upstream_status == 200 else upstream_status
    return JSONResponse(content={"items": merged}, status_code=status_code)
