"""Content-addressed cache for skill blobs on the scheduler host.

Stage-2 block B (per design in docs/design/skills-host-mount-v1.md):
spawn-time, the scheduler asks ``skill-store`` to resolve a list of
skill names → ``{name, version, sha256, blob_url}``; for each entry we
ensure the unpacked skill directory exists at ``{cache_base}/{sha256}/``
and then hand the host path back to the runtime so it can be
**read-only bind-mounted** into the sandbox.

Design constraints
------------------
* **failure-open**: any error materialising a skill is logged and
  silently dropped — the sandbox just sees that skill absent.  We never
  fail a spawn over a single bad skill, because the agent run is more
  valuable than perfect skill availability.
* **content-addressed**: the directory name is the sha256 of the
  uploaded blob, so two different skill names that pin the same blob
  (or one skill that was install-pinned at v3 and v4 where v3 is later
  re-pinned by another user) share the same on-disk layout.
* **cross-process safe**: spawns can run in parallel; an ``fcntl.flock``
  on a per-sha lock file keeps two workers from racing to extract into
  the same target.  The double-check-after-lock pattern means the second
  one short-circuits as soon as it sees the target appear.
* **anti-bomb / anti-traversal**: we reuse the skill-store decompressed
  caps (2000 members / 5 MiB per member / 50 MiB total) and rely on
  Python 3.12+ ``tarfile.data_filter`` to refuse symlinks, absolute
  paths, ``..`` segments and device files.
"""

from __future__ import annotations

import fcntl
import hashlib
import logging
import os
import re
import shutil
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Mirror skill-store skill_package.py constants — kept in sync by hand
# rather than imported across services to preserve service isolation.
_MAX_MEMBERS = 2000
_MAX_MEMBER_SIZE = 5 * 1024 * 1024  # 5 MiB
_MAX_TOTAL_SIZE = 50 * 1024 * 1024  # 50 MiB

# Skill name whitelist — anything that gets concatenated into the
# in-sandbox mount path ``/home/skills/<name>``.  Letters/digits and
# ``._-`` only; everything else is dropped with a warning so a malicious
# or buggy upstream can't slip a ``..`` or shell metachar through.
_SAFE_SKILL_NAME = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


@dataclass(frozen=True)
class MountedSkill:
    """One ready-to-mount skill on the scheduler host.

    ``host_path`` is the absolute path of the extracted skill root on
    the scheduler host; the runtime bind-mounts it read-only at
    ``/home/skills/<name>`` inside the sandbox.  The path is identical
    on the docker daemon's host because compose binds the cache dir
    with the same source and target (see agent-platform.yml).
    """

    name: str
    sha256: str
    host_path: str


class SkillCache:
    """Resolve + materialise skill blobs on the scheduler host.

    Constructed once per ``DockerRuntime`` / ``CubeSandboxRuntime`` and
    reused across spawns.  Thread-safe at the ``materialize`` level via
    fcntl file locks; the underlying ``httpx.Client`` is sync to match
    the runtime's spawn path.
    """

    def __init__(
        self,
        *,
        cache_base: str,
        skill_store_url: str,
        service_scopes: str,
        timeout: float = 30.0,
    ) -> None:
        self._cache_base = Path(cache_base)
        self._skill_store_url = skill_store_url.rstrip("/")
        self._service_scopes = service_scopes
        self._client = httpx.Client(timeout=timeout)

    # ------------------------------------------------------------------ public
    def materialize(
        self,
        skills: list[str],
        *,
        tenant_id: str,
        user_id: str,
    ) -> list[MountedSkill]:
        """Resolve ``skills`` and ensure each blob is unpacked on disk.

        Returns one :class:`MountedSkill` per skill that's available on
        disk after this call.  Skills that fail to resolve / download /
        validate are dropped silently (with a log line) — see
        module docstring on failure-open.
        """
        if not skills:
            return []
        if not self._skill_store_url:
            logger.warning(
                "skill_cache: skill_store_url not configured; "
                "skipping skill materialisation (failure-open)",
            )
            return []

        try:
            self._cache_base.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning(
                "skill_cache: cannot create cache_base; skipping materialisation",
                extra={"cache_base": str(self._cache_base), "err": str(exc)},
            )
            return []

        resolved = self._resolve(skills, tenant_id=tenant_id, user_id=user_id)
        mounted: list[MountedSkill] = []
        for entry in resolved:
            name = str(entry.get("name") or "")
            sha = str(entry.get("sha256") or "")
            blob_url = str(entry.get("blob_url") or "")
            if not (name and sha and blob_url):
                logger.warning(
                    "skill_cache: skipping malformed resolve entry",
                    extra={"entry": entry},
                )
                continue
            if not _SAFE_SKILL_NAME.match(name):
                logger.warning(
                    "skill_cache: skipping skill with unsafe name",
                    extra={"skill_name": name},
                )
                continue
            if not re.fullmatch(r"[0-9a-f]{64}", sha):
                logger.warning(
                    "skill_cache: skipping skill with malformed sha256",
                    extra={"skill_name": name, "sha": sha},
                )
                continue
            try:
                ms = self._materialize_one(name=name, sha256=sha, blob_url=blob_url)
            except Exception as exc:  # noqa: BLE001 -- failure-open per skill
                logger.warning(
                    "skill_cache: materialise failed (failure-open)",
                    extra={"skill_name": name, "sha": sha, "err": str(exc)},
                )
                continue
            if ms is not None:
                mounted.append(ms)
        return mounted

    # ------------------------------------------------------------------ resolve
    def _resolve(
        self,
        skills: list[str],
        *,
        tenant_id: str,
        user_id: str,
    ) -> list[dict[str, Any]]:
        url = f"{self._skill_store_url}/internal/skills/resolve"
        names = ",".join(skills)
        headers = {
            "X-Service-Scopes": self._service_scopes,
            "X-Tenant-Id": tenant_id,
        }
        params = {"tenant_id": tenant_id, "user_id": user_id, "names": names}
        try:
            resp = self._client.get(url, params=params, headers=headers)
        except httpx.HTTPError as exc:
            logger.warning(
                "skill_cache: resolve request failed (failure-open)",
                extra={"url": url, "err": str(exc)},
            )
            return []
        if resp.status_code >= 400:
            logger.warning(
                "skill_cache: resolve returned non-2xx (failure-open)",
                extra={"url": url, "status": resp.status_code},
            )
            return []
        try:
            payload = resp.json()
        except ValueError:
            logger.warning("skill_cache: resolve returned non-JSON body")
            return []
        items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            return []
        return items

    # ------------------------------------------------------------------ per-skill
    def _materialize_one(
        self,
        *,
        name: str,
        sha256: str,
        blob_url: str,
    ) -> MountedSkill | None:
        target = self._cache_base / sha256
        if _target_ready(target):
            return MountedSkill(name=name, sha256=sha256, host_path=str(target))

        lock_path = self._cache_base / f".lock-{sha256}"
        # ``fcntl.flock`` LOCK_EX is cross-process advisory; identical-host
        # concurrent spawns serialise here.  Open in append mode so the lock
        # file doesn't get truncated by repeated openers.
        with open(lock_path, "a") as lock_fh:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
            try:
                # Re-check inside the lock: another process may have raced us
                # and finished the extraction while we were waiting.
                if _target_ready(target):
                    return MountedSkill(name=name, sha256=sha256, host_path=str(target))

                # Pull the blob.  Predigned URL — do NOT add auth header.
                # See [[skill-internal-presign-and-no-bearer]].
                try:
                    resp = self._client.get(blob_url)
                except httpx.HTTPError as exc:
                    logger.warning(
                        "skill_cache: blob download failed",
                        extra={"skill_name": name, "sha": sha256, "err": str(exc)},
                    )
                    return None
                if resp.status_code >= 400:
                    logger.warning(
                        "skill_cache: blob download non-2xx",
                        extra={"skill_name": name, "sha": sha256, "status": resp.status_code},
                    )
                    return None
                data = resp.content

                actual = hashlib.sha256(data).hexdigest()
                if actual != sha256:
                    logger.warning(
                        "skill_cache: sha256 mismatch (skip)",
                        extra={"skill_name": name, "expected": sha256, "actual": actual},
                    )
                    return None

                # Extract into a tmp dir then atomic rename to the final
                # content-addressed target.  Anti-bomb checks first (header
                # walk, no decompression yet), then ``extractall`` with the
                # data filter for traversal/symlink defence.
                tmp_extract = Path(
                    tempfile.mkdtemp(
                        prefix=f".tmp-extract-{sha256}-",
                        dir=str(self._cache_base),
                    )
                )
                ok = False
                try:
                    if not _safe_extract(data, tmp_extract):
                        return None
                    try:
                        os.rename(tmp_extract, target)
                    except OSError as exc:
                        # Lost the rename race (or filesystem quirk).  If
                        # target now exists and is valid, treat as success.
                        if _target_ready(target):
                            return MountedSkill(
                                name=name, sha256=sha256, host_path=str(target),
                            )
                        logger.warning(
                            "skill_cache: atomic rename failed",
                            extra={"skill_name": name, "sha": sha256, "err": str(exc)},
                        )
                        return None
                    ok = True
                    return MountedSkill(
                        name=name, sha256=sha256, host_path=str(target),
                    )
                finally:
                    if not ok and tmp_extract.exists():
                        shutil.rmtree(tmp_extract, ignore_errors=True)
            finally:
                fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)


# --------------------------------------------------------------------- helpers
def _target_ready(target: Path) -> bool:
    """A target is ready when SKILL.md is present at the root or one level deep.

    Mirrors the layout skill-store accepts at upload time (``SKILL.md`` at
    archive root or ``<dir>/SKILL.md`` for a single subdirectory layout).
    """
    if not target.is_dir():
        return False
    if (target / "SKILL.md").is_file():
        return True
    for child in target.iterdir():
        if child.is_dir() and (child / "SKILL.md").is_file():
            return True
    return False


def _safe_extract(data: bytes, dest: Path) -> bool:
    """Anti-bomb + anti-traversal extraction of a gzip tarball.

    Returns True on success, False if the archive violates any cap or
    the extraction step itself raises.  We log warnings for the
    rejected cases so operators can spot a misbehaving uploader; the
    sandbox just sees the skill absent.
    """
    import io

    try:
        tar = tarfile.open(fileobj=io.BytesIO(data), mode="r:gz")  # noqa: SIM115
    except (tarfile.TarError, OSError, EOFError) as exc:
        logger.warning("skill_cache: not a gzip tarball", extra={"err": str(exc)})
        return False

    with tar:
        try:
            members = tar.getmembers()
        except (tarfile.TarError, OSError, EOFError) as exc:
            logger.warning("skill_cache: tar getmembers failed", extra={"err": str(exc)})
            return False

        if len(members) > _MAX_MEMBERS:
            logger.warning(
                "skill_cache: too many members",
                extra={"count": len(members), "cap": _MAX_MEMBERS},
            )
            return False

        total = 0
        for m in members:
            if m.issym() or m.islnk() or m.isdev():
                logger.warning(
                    "skill_cache: rejecting special tar member",
                    extra={"skill_name": m.name},
                )
                return False
            if m.name.startswith("/") or ".." in m.name.replace("\\", "/").split("/"):
                logger.warning(
                    "skill_cache: rejecting unsafe tar path",
                    extra={"skill_name": m.name},
                )
                return False
            if m.size > _MAX_MEMBER_SIZE:
                logger.warning(
                    "skill_cache: rejecting oversized member",
                    extra={"skill_name": m.name, "size": m.size},
                )
                return False
            total += m.size
            if total > _MAX_TOTAL_SIZE:
                logger.warning(
                    "skill_cache: rejecting archive — total size cap exceeded",
                    extra={"total": total},
                )
                return False

        try:
            # ``filter="data"`` is available in Python 3.12+ and refuses
            # symlinks, absolute paths and device files.  On older runtimes
            # the explicit member walk above is the safety net.
            tar.extractall(path=dest, filter="data")  # type: ignore[arg-type]
        except (tarfile.TarError, OSError) as exc:
            logger.warning(
                "skill_cache: extractall failed",
                extra={"err": str(exc)},
            )
            return False
        except TypeError:
            # Pre-3.12 fallback — no filter kwarg.
            try:
                tar.extractall(path=dest)  # noqa: S202 -- header walk above guards
            except (tarfile.TarError, OSError) as exc:
                logger.warning(
                    "skill_cache: extractall (no filter) failed",
                    extra={"err": str(exc)},
                )
                return False

    return True
