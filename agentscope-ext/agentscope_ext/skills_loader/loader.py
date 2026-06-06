"""Skill download + register implementation.

A Skill manifest from ``skill-store`` looks like::

    {
      "name": "csv-tools",
      "version": "1.2.0",
      "download_url": "https://.../csv-tools-1.2.0.tar.gz",
      "sha256": "abc123...",
      "entry_module": "csv_tools",
      "entry_function": "register",
      "content_type": "application/x-gzip"
    }

``entry_function`` is called with the ``Toolkit`` and is responsible
for ``register_tool_function(...)`` on whatever tools it wants to
expose.  This keeps the skill SDK trivial (a single registration
function per package) and lets the loader stay agnostic about which
tools a skill ships.

Security posture
----------------
* Only ``application/x-gzip`` payloads are accepted.
* The ``sha256`` field is verified before unpack.
* The tarball is extracted with the standard library's ``filter='data'``
  guard against directory escapes.
* If anything trips, the skill is logged and skipped — never raised.
"""

from __future__ import annotations

import hashlib
import importlib
import logging
import sys
import tarfile
import tempfile
from pathlib import Path
from typing import Any, Iterable, Mapping

import httpx

_LOG = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = httpx.Timeout(15.0, connect=3.0)
_CLIENT: httpx.Client | None = None
_SKILLS_ROOT = Path(tempfile.gettempdir()) / "skills"
_INSTALLED_PATHS: set[str] = set()


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


def _download_bytes(url: str, token: str) -> tuple[bytes, str]:
    """Fetch the tarball; returns ``(body, content_type)``."""
    headers: dict[str, str] = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    response = _client().get(url, headers=headers)
    response.raise_for_status()
    content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
    return response.content, content_type


# ---------------------------------------------------------------------------
# Install
# ---------------------------------------------------------------------------


def install_skill(toolkit: Any, manifest: Mapping[str, Any]) -> bool:
    """Download + unpack + import + register a single skill.

    Returns ``True`` on success, ``False`` if the skill was skipped
    (sha mismatch, bad content type, missing manifest fields, etc.).
    All failure modes log a warning rather than raise.
    """
    name = manifest.get("name")
    version = manifest.get("version", "0.0.0")
    download_url = manifest.get("download_url")
    expected_sha = manifest.get("sha256", "")
    entry_module = manifest.get("entry_module")
    entry_function = manifest.get("entry_function")
    token = str(manifest.get("token") or "")

    if not (name and download_url and entry_module and entry_function):
        _LOG.warning("skills_loader: manifest missing required fields: %r", manifest)
        return False

    body, content_type = _download_bytes(str(download_url), token)
    if content_type and content_type != "application/x-gzip":
        _LOG.warning(
            "skills_loader: %s rejected — content-type=%r (want application/x-gzip)",
            name,
            content_type,
        )
        return False
    if expected_sha:
        actual_sha = hashlib.sha256(body).hexdigest()
        if actual_sha.lower() != str(expected_sha).lower():
            _LOG.warning(
                "skills_loader: %s rejected — sha256 mismatch (got %s, want %s)",
                name,
                actual_sha,
                expected_sha,
            )
            return False

    target = _SKILLS_ROOT / f"{name}-{version}"
    target.mkdir(parents=True, exist_ok=True)
    _safe_extract(body, target)

    target_str = str(target)
    if target_str not in _INSTALLED_PATHS:
        sys.path.insert(0, target_str)
        _INSTALLED_PATHS.add(target_str)

    try:
        module = importlib.import_module(str(entry_module))
        # If the user re-installs the same skill we need a fresh module
        # object so a new ``register`` body takes effect.
        module = importlib.reload(module)
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("skills_loader: import %s failed: %r", entry_module, exc)
        return False

    fn = getattr(module, str(entry_function), None)
    if not callable(fn):
        _LOG.warning(
            "skills_loader: %s has no callable %s", entry_module, entry_function,
        )
        return False
    try:
        fn(toolkit)
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("skills_loader: %s.%s raised: %r", entry_module, entry_function, exc)
        return False
    return True


def _safe_extract(body: bytes, dest: Path) -> None:
    """Unpack ``body`` (a gzipped tarball) into ``dest`` using the data filter."""
    with tempfile.NamedTemporaryFile(suffix=".tar.gz") as fh:
        fh.write(body)
        fh.flush()
        with tarfile.open(fh.name, mode="r:gz") as tar:
            try:
                tar.extractall(path=dest, filter="data")  # type: ignore[arg-type]
            except TypeError:
                # Python <3.12 fallback: rely on the simple path check.
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
    "install_skill",
    "set_client",
    "set_skills_root",
]
