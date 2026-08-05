"""Shared run-output upload for the sandbox runtimes.

Both runtimes end a run the same way: a per-run host directory (docker bind
mount / cube host-mount) holds whatever the agent wrote.  This module scans
that directory once, uploads every output file to S3, and returns two
categorised lists the orchestrator consumes independently:

* **workspace_files** — every output file; the orchestrator's
  ``process_writeback`` creates/updates ``WorkspaceFile`` rows so the file
  appears in the workspace file list.
* **artifacts** — the subset written to ``outputs/``; the orchestrator
  persists ``Attachment(kind=output)`` rows so the chat UI shows output-file
  cards for the deliverables the agent explicitly placed there.

Both lists reference the **same S3 blob** (single upload, no duplication).
"""

from __future__ import annotations

import logging
import mimetypes
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Control-plane files that share the run directory but are never user output.
#
# ``.secrets.json`` is normally read-then-unlinked by the worker; listing it
# here means a worker that dies before unlinking still cannot leak its secrets
# into object storage.  ``worker.log`` is the cube runtime's stdout mirror —
# diagnostics for us, noise for the user.
EXCLUDED_NAMES: frozenset[str] = frozenset({
    "task.json",
    "result.json",
    "events.jsonl",
    "canvas.json",
    ".secrets.json",
    "worker.log",
})


# Extensions ``mimetypes`` does not know, which would otherwise be stored as
# application/octet-stream — and a binary MIME makes the UI treat a perfectly
# readable script as undisplayable.
_EXTRA_MIMES: dict[str, str] = {
    ".m": "text/x-matlab",
    ".jl": "text/x-julia",
    ".r": "text/x-r",
    ".ts": "text/x-typescript",
    ".tsx": "text/x-typescript",
    ".toml": "text/plain",
    ".rs": "text/x-rust",
    ".go": "text/x-go",
}


def guess_mime(filename: str) -> str:
    """MIME for an output file, falling back to octet-stream."""
    mime, _ = mimetypes.guess_type(filename)
    if mime:
        return mime
    dot = filename.rfind(".")
    if dot >= 0:
        return _EXTRA_MIMES.get(filename[dot:].lower(), "application/octet-stream")
    return "application/octet-stream"


def make_s3_client(settings: Any) -> Any | None:
    """Build a boto3 S3 client using S3_* settings (not AWS_* env vars).

    Returns None if boto3 is unavailable or credentials are unset.
    """
    try:
        import boto3

        return boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint_url,
            region_name=settings.s3_region,
            aws_access_key_id=settings.s3_access_key_id,
            aws_secret_access_key=settings.s3_secret_access_key,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("S3 client unavailable: %s", exc)
        return None


def upload_run_output_files(
    *,
    workspace: Path,
    tenant_id: str,
    session_id: str,
    run_id: str,
    workspace_id: str,
    bucket: str,
    s3_client: Any | None,
    excluded_names: frozenset[str] = EXCLUDED_NAMES,
) -> dict[str, list[dict[str, Any]]]:
    """Scan the per-run workspace for output files, upload once, return two lists.

    Returns::

        {
            "workspace_files": [          # ALL output files → workspace persistence
                {"filename": str, "s3_key": str, "size_bytes": int, "mime": str},
                ...
            ],
            "artifacts": [                # outputs/ subset → chat output-file cards
                {"filename": str, "s3_key": str, "size": int, "mime": str},
                ...
            ],
        }

    When ``workspace_id`` is non-empty the S3 key uses the workspace namespace
    (``tenant/{tid}/workspaces/{wid}/files/...``) so the blob is owned by the
    workspace.  When empty the legacy session/run namespace is used and only
    ``artifacts`` is populated (no workspace to write back to).

    Skipped: control-plane files in ``excluded_names`` (root level only, so an
    agent's own ``outputs/result.json`` still ships) and everything under
    ``inputs/`` (user-uploaded originals, already stored).

    Failure-open: individual file errors are logged and skipped.
    """
    if not workspace.is_dir() or s3_client is None:
        return {"workspace_files": [], "artifacts": []}

    workspace_files: list[dict[str, Any]] = []
    artifacts: list[dict[str, Any]] = []
    inputs_dir = workspace / "inputs"
    outputs_dir = workspace / "outputs"

    for fpath in sorted(workspace.rglob("*")):
        if not fpath.is_file():
            continue
        # Skip inputs/ subdirectory.
        try:
            fpath.relative_to(inputs_dir)
            continue
        except ValueError:
            pass
        # Skip system files at workspace root.
        if fpath.parent == workspace and fpath.name in excluded_names:
            continue

        relpath = fpath.relative_to(workspace).as_posix()

        if workspace_id:
            s3_key = (
                f"tenant/{tenant_id}/workspaces/{workspace_id}"
                f"/files/{run_id}/{relpath}"
            )
        else:
            s3_key = (
                f"tenant/{tenant_id}/sessions/{session_id}"
                f"/runs/{run_id}/artifacts/{relpath}"
            )

        try:
            data = fpath.read_bytes()
            mime = guess_mime(fpath.name)
            s3_client.put_object(
                Bucket=bucket,
                Key=s3_key,
                Body=data,
                ContentType=mime,
            )
            logger.info(
                "output file uploaded",
                extra={"s3_key": s3_key, "size": len(data), "run_id": run_id},
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "output file upload failed (failure-open)",
                extra={"path": str(fpath), "err": str(exc)},
            )
            continue

        # workspace_files: everything (writeback to workspace)
        if workspace_id:
            workspace_files.append({
                "filename": relpath,
                "blob_key": s3_key,
                "size_bytes": len(data),
                "mime": mime,
            })

        # artifacts: only outputs/ files (chat output-file cards)
        is_output = False
        try:
            fpath.relative_to(outputs_dir)
            is_output = True
        except ValueError:
            pass
        if is_output:
            artifacts.append({
                "filename": fpath.name,
                "s3_key": s3_key,
                "size": len(data),
                "mime": mime,
            })

    return {"workspace_files": workspace_files, "artifacts": artifacts}
