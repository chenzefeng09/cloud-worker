"""Skill download + unpack implementation (progressive disclosure).

A Skill version manifest from ``skill-store`` is the server-authoritative,
fixed 5-key document::

    {
      "name": "poet",
      "description": "writes poetry",
      "skill_md_path": "SKILL.md",
      "format": "skill_md_v1",
      "sha256": "<hex of the uploaded tarball bytes>"
    }

The internal endpoint (``GET /internal/skills``) returns one entry per skill,
each carrying a ``versions`` list whose items expose ``manifest`` (the 5-key
doc above), ``blob_url`` (the download URL) and ``version``.

Consumption model
-----------------
The worker no longer *executes* any user-uploaded code.  Instead each skill
package is unpacked into ``/workspace/skills/<name>-<version>/`` and the agent
is told — via the system prompt — the skill ``name``/``description`` and the
absolute path to its ``SKILL.md``.  The agent then uses the ``Read`` tool to
pull the full instructions (and any sibling ``scripts``/``references``) on
demand (progressive disclosure).

Security posture
----------------
* The ``sha256`` field is **mandatory** and verified against the downloaded
  bytes before unpack — a mismatch skips the skill.
* The tarball is extracted with the standard library's ``filter='data'``
  guard against directory escapes (with a manual fallback on Python <3.12).
* Member-count / per-file / total-size caps provide defence-in-depth against
  archive bombs.
* If anything trips, the skill is logged and skipped — never raised.
"""

from __future__ import annotations

import hashlib
import logging
import tarfile
import tempfile
from pathlib import Path
from typing import Any, Iterable, Mapping

import httpx

_LOG = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = httpx.Timeout(15.0, connect=3.0)
_CLIENT: httpx.Client | None = None
_SKILLS_ROOT = Path("/workspace") / "skills"

# Defence-in-depth unpack limits (kept in sync with skill-store block-1 caps;
# defined locally rather than imported across services).
_MAX_MEMBERS = 2000
_MAX_FILE_BYTES = 5 * 1024 * 1024  # 5 MiB per member
_MAX_TOTAL_BYTES = 50 * 1024 * 1024  # 50 MiB total uncompressed


def set_client(client: httpx.Client | None) -> None:
    """Override the module-level ``httpx.Client`` (for tests)."""
    global _CLIENT
    _CLIENT = client


def set_skills_root(path: Path | str) -> None:
    """Override where skills get extracted to.  Mainly for tests."""
    global _SKILLS_ROOT
    _SKILLS_ROOT = Path(path)


def _client() -> httpx.Client:
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = httpx.Client(timeout=_DEFAULT_TIMEOUT)
    return _CLIENT


# ---------------------------------------------------------------------------
# Network — manifest + bytes
# ---------------------------------------------------------------------------


def fetch_skill_manifests(
    *,
    base_url: str,
    token: str,
    tenant_id: str,
    names: Iterable[str],
    service_scopes: str = "",
) -> list[dict[str, Any]]:
    """GET ``{base_url}/internal/skills?tenant_id=...&names=...``.

    ``base_url`` should be the skill-store service base URL including the
    API version prefix, e.g. ``http://skill-store:8011/api/skills/v1``.
    The caller (``skills_loader.__init__``) resolves this from
    ``SKILL_STORE_URL`` env (preferred) or falls back to constructing
    it from ``FOUNDATION_API_URL``.

    ``service_scopes`` is the value for the ``X-Service-Scopes`` header that
    the internal endpoint requires.  In production the API gateway sets this
    header after verifying the Bearer JWT; in dev (no gateway) the scheduler
    injects it via the ``FOUNDATION_SERVICE_SCOPES`` env var so the sandbox
    can assert the scopes directly.  Pass the empty string to omit the header
    (will result in a 403 from the service).

    Returns the raw skill entries (each with a ``versions`` list).  Callers
    flatten to a per-version download target via :func:`select_version`.
    """
    name_list = [n for n in names if n]
    if not name_list:
        return []
    if not base_url:
        raise httpx.InvalidURL("SKILL_STORE_URL / FOUNDATION_API_URL is empty")
    url = base_url.rstrip("/") + "/internal/skills"
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if tenant_id:
        headers["X-Tenant-Id"] = tenant_id
    if service_scopes:
        headers["X-Service-Scopes"] = service_scopes
    params = [("tenant_id", tenant_id)] + [("names", n) for n in name_list]
    response = _client().get(url, params=params, headers=headers)
    response.raise_for_status()
    body = response.json()
    if isinstance(body, list):
        return list(body)
    if isinstance(body, dict):
        items = body.get("skills") or body.get("items") or []
        if isinstance(items, list):
            return list(items)
    return []


def select_version(skill: Mapping[str, Any]) -> dict[str, Any] | None:
    """Flatten one skill entry into a single install target.

    Picks the last version in the ``versions`` list (the internal endpoint
    returns them upload-ordered) and merges its 5-key ``manifest`` with the
    version-level ``blob_url`` / ``version`` / ``sha256``.  Returns ``None``
    (caller skips) if the entry has no usable version.
    """
    versions = skill.get("versions")
    if isinstance(versions, list) and versions:
        ver = versions[-1]
        if isinstance(ver, Mapping):
            manifest = dict(ver.get("manifest") or {})
            blob_url = ver.get("blob_url") or ver.get("download_url")
            sha256 = ver.get("sha256") or manifest.get("sha256")
            out: dict[str, Any] = {
                "name": manifest.get("name") or skill.get("name"),
                "description": manifest.get("description")
                or skill.get("description")
                or "",
                "skill_md_path": manifest.get("skill_md_path") or "SKILL.md",
                "version": ver.get("version") or "0.0.0",
                "blob_url": blob_url,
                "sha256": sha256,
            }
            return out
    # Tolerate an already-flat manifest (e.g. tests passing a flat dict).
    blob_url = skill.get("blob_url") or skill.get("download_url")
    if skill.get("name") and blob_url:
        return {
            "name": skill.get("name"),
            "description": skill.get("description") or "",
            "skill_md_path": skill.get("skill_md_path") or "SKILL.md",
            "version": skill.get("version") or "0.0.0",
            "blob_url": blob_url,
            "sha256": skill.get("sha256"),
        }
    return None


def _download_bytes(url: str, token: str) -> bytes:
    """Fetch the tarball bytes.

    If ``url`` is an S3 presigned URL (auth carried in query string),
    do NOT add an ``Authorization`` header — minio/S3 reject requests
    that mix presigned-query and bearer-header auth with 400.
    """
    headers: dict[str, str] = {}
    is_presigned = ("AWSAccessKeyId=" in url) or ("X-Amz-Signature=" in url)
    if token and not is_presigned:
        headers["Authorization"] = f"Bearer {token}"
    response = _client().get(url, headers=headers)
    response.raise_for_status()
    return response.content


# ---------------------------------------------------------------------------
# Install (unpack only — no user code is ever imported/executed)
# ---------------------------------------------------------------------------


def install_skill(manifest: Mapping[str, Any], *, token: str = "") -> dict[str, Any] | None:
    """Download + verify + unpack a single skill package.

    ``manifest`` is a flattened install target (see :func:`select_version`):
    ``{name, description, skill_md_path, version, blob_url, sha256}``.

    Returns ``{name, description, skill_md_abspath}`` on success, or ``None``
    if the skill was skipped (missing fields, sha mismatch, bad archive,
    missing ``SKILL.md``).  All failure modes log a warning rather than raise.
    """
    name = manifest.get("name")
    version = manifest.get("version", "0.0.0")
    blob_url = manifest.get("blob_url") or manifest.get("download_url")
    description = str(manifest.get("description") or "")
    skill_md_path = str(manifest.get("skill_md_path") or "SKILL.md")
    expected_sha = manifest.get("sha256")

    if not (name and blob_url):
        _LOG.warning("skills_loader: manifest missing name/blob_url: %r", manifest)
        return None
    if not expected_sha:
        _LOG.warning("skills_loader: %s rejected — manifest has no sha256", name)
        return None

    try:
        body = _download_bytes(str(blob_url), token)
    except Exception as exc:  # noqa: BLE001 -- failure-open per skill
        _LOG.warning("skills_loader: download %s failed: %r", name, exc)
        return None

    actual_sha = hashlib.sha256(body).hexdigest()
    if actual_sha.lower() != str(expected_sha).lower():
        _LOG.warning(
            "skills_loader: %s rejected — sha256 mismatch (got %s, want %s)",
            name,
            actual_sha,
            expected_sha,
        )
        return None

    target = _SKILLS_ROOT / f"{name}-{version}"
    try:
        target.mkdir(parents=True, exist_ok=True)
        _safe_extract(body, target)
    except Exception as exc:  # noqa: BLE001 -- bad archive / path escape / bomb
        _LOG.warning("skills_loader: unpack %s failed: %r", name, exc)
        return None

    skill_md_abspath = (target / skill_md_path).resolve()
    # Defence: the resolved SKILL.md must stay inside the skill dir.
    try:
        skill_md_abspath.relative_to(target.resolve())
    except ValueError:
        _LOG.warning(
            "skills_loader: %s rejected — skill_md_path escapes dir: %r",
            name,
            skill_md_path,
        )
        return None
    if not skill_md_abspath.is_file():
        _LOG.warning(
            "skills_loader: %s rejected — SKILL.md not found at %s",
            name,
            skill_md_abspath,
        )
        return None

    return {
        "name": str(name),
        "description": description,
        "skill_md_abspath": str(skill_md_abspath),
    }


def _safe_extract(body: bytes, dest: Path) -> None:
    """Unpack ``body`` (a gzipped tarball) into ``dest`` with depth limits."""
    with tempfile.NamedTemporaryFile(suffix=".tar.gz") as fh:
        fh.write(body)
        fh.flush()
        with tarfile.open(fh.name, mode="r:gz") as tar:
            members = tar.getmembers()
            if len(members) > _MAX_MEMBERS:
                raise ValueError(
                    f"too many members: {len(members)} > {_MAX_MEMBERS}"
                )
            total = 0
            for member in members:
                if member.size > _MAX_FILE_BYTES:
                    raise ValueError(
                        f"member {member.name!r} too large: {member.size} bytes"
                    )
                total += member.size
                if total > _MAX_TOTAL_BYTES:
                    raise ValueError(
                        f"archive too large uncompressed: {total} bytes"
                    )
            try:
                tar.extractall(path=dest, filter="data")  # type: ignore[arg-type]
            except TypeError:
                # Python <3.12 fallback: rely on the manual path check.
                _legacy_extract(tar, dest)


def _legacy_extract(tar: tarfile.TarFile, dest: Path) -> None:
    resolved = dest.resolve()
    for member in tar.getmembers():
        target = (dest / member.name).resolve()
        if not str(target).startswith(str(resolved)):
            raise ValueError(f"unsafe path in tarball: {member.name!r}")
    tar.extractall(path=dest)


__all__ = [
    "fetch_skill_manifests",
    "select_version",
    "install_skill",
    "set_client",
    "set_skills_root",
]
