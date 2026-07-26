"""Shared run-artifact upload for the sandbox runtimes.

Both runtimes end a run the same way: a per-run host directory (docker bind
mount / cube host-mount) holds whatever the agent wrote, and the orchestrator
wants that turned into an ``artifacts`` list.  Keeping the scan, the exclusion
list and the S3 key layout here means the two runtimes cannot drift on which
files get published — a drift that previously left cube runs with no artifacts
at all, so assistant replies referencing ``![chart](chart.png)`` rendered as
"image not found" in the UI.
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


def upload_run_artifacts(
    *,
    workspace: Path,
    tenant_id: str,
    session_id: str,
    run_id: str,
    bucket: str,
    s3_client: Any | None,
    excluded_names: frozenset[str] = EXCLUDED_NAMES,
) -> list[dict[str, Any]]:
    """Scan ``workspace`` for output files and upload them to S3.

    Skipped: the control-plane files in ``excluded_names`` (root level only, so
    an agent's own ``outputs/result.json`` still ships) and everything under
    ``inputs/`` (user-uploaded originals, already stored).  Returns::

        [{"filename": str, "s3_key": str, "size": int, "mime": str}, ...]

    Failure-open: individual file errors are logged and skipped.
    """
    if not workspace.is_dir() or s3_client is None:
        return []

    artifacts: list[dict[str, Any]] = []
    inputs_dir = workspace / "inputs"

    for fpath in sorted(workspace.rglob("*")):
        if not fpath.is_file():
            continue
        # Skip inputs/ subdirectory.
        try:
            fpath.relative_to(inputs_dir)
            continue  # inside inputs/ — skip
        except ValueError:
            pass
        # Skip system files at workspace root.
        if fpath.parent == workspace and fpath.name in excluded_names:
            continue

        relpath = fpath.relative_to(workspace).as_posix()
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
            artifacts.append({
                "filename": fpath.name,
                "s3_key": s3_key,
                "size": len(data),
                "mime": mime,
            })
            logger.info(
                "artifact uploaded",
                extra={"s3_key": s3_key, "size": len(data), "run_id": run_id},
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "artifact upload failed (failure-open)",
                extra={"path": str(fpath), "err": str(exc)},
            )

    return artifacts
