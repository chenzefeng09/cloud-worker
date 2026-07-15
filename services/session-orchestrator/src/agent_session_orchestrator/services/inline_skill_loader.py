"""Inline-path skill loader — resolves a run's skills and fetches their files.

Background
----------
Skills are pure instruction packages served by **skill-store**.  The inline run
executor advertises a ``read_skill`` tool (see ``skill_tool_spec``) so the model
can pull a skill's ``SKILL.md`` (or a reference file) on demand.  This module is
the skill-store client behind that flow:

* :func:`resolve_skills` — turn a list of skill names into a catalog of
  ``{name, version, sha256, description}`` entries (one ``resolve`` call per
  name so a single bad skill can be skipped without dropping the rest).  The
  catalog feeds the system-prompt block and gates whether ``read_skill`` is
  advertised at all.  Resolve is called **once per run** and never cached: a
  skill upgraded between turns must surface its new ``sha256`` immediately.
* :func:`fetch_skill_file` — fetch one package file's UTF-8 body from the file
  endpoint, backed by a bounded content-addressed LRU (key ``(sha256, path)``)
  so re-reads within a process are free and never stale (the sha changes when
  the content does).

Failure philosophy
------------------
Both functions are **failure-open**, mirroring the scheduler's ``skill_cache``:
a skill-store outage or a single malformed skill must degrade the run (skip the
skill, return an error string the model can read) — never fail it.
"""

from __future__ import annotations

import logging
from collections import OrderedDict

import httpx

logger = logging.getLogger(__name__)

# Truncate a fetched skill file at this many bytes (UTF-8) and append a Chinese
# truncation notice.  SKILL.md files are progressive-disclosure entry points and
# should be small; a runaway file must not blow up the completion context.
_SKILL_FILE_MAX_BYTES = 64 * 1024

_TRUNCATION_NOTICE = "\n\n【提示】技能文件内容过长，已截断，仅显示前 64KB。"

# Content-addressed LRU for fetched skill files.  Key is ``(sha256, path)``:
# because the key includes the content hash it never needs invalidation — a
# changed file yields a different sha256 and therefore a different key.  Bounded
# so a long-lived process holding many skills can't grow unboundedly.
_FILE_CACHE_MAX_ENTRIES = 256
_file_cache: OrderedDict[tuple[str, str], str] = OrderedDict()


class SkillCatalogEntry(dict):
    """A resolved skill catalog entry: ``{name, version, sha256, description}``.

    A thin ``dict`` subclass (not a dataclass) so it serialises/compares like the
    plain dicts the rest of the inline path passes around, while giving callers a
    named type to annotate against.
    """

    @property
    def name(self) -> str:
        return str(self.get("name") or "")

    @property
    def sha256(self) -> str:
        return str(self.get("sha256") or "")


def _skill_store_headers(*, service_scopes: str, tenant_id: str, user_id: str) -> dict[str, str]:
    """Headers for a skill-store internal read (mirror of ``_fetch_default_installed_skills``)."""
    return {
        "X-Tenant-Id": tenant_id,
        "X-User-Id": user_id or "SYSTEM",
        "X-Service-Scopes": service_scopes,
    }


async def _resolve_one(
    *,
    client: httpx.AsyncClient,
    skill_store_url: str,
    service_scopes: str,
    tenant_id: str,
    user_id: str,
    name: str,
) -> SkillCatalogEntry | None:
    """Resolve a single skill name → catalog entry, or ``None`` on any failure.

    Calls ``GET /internal/skills/resolve?names=<name>`` (the existing endpoint
    takes a comma-separated ``names``; one name per call keeps each skill's
    failure isolated).  Fails open: a non-2xx / malformed / absent result logs
    a warning and returns ``None`` so the caller simply drops this skill.
    """
    url = f"{skill_store_url.rstrip('/')}/internal/skills/resolve"
    try:
        resp = await client.get(
            url,
            params={"tenant_id": tenant_id, "user_id": user_id, "names": name},
            headers=_skill_store_headers(
                service_scopes=service_scopes, tenant_id=tenant_id, user_id=user_id
            ),
        )
    except httpx.HTTPError as exc:
        logger.warning(
            "inline skill resolve request failed (failure-open): %s",
            exc,
            extra={"skill_name": name},
        )
        return None
    if resp.status_code >= 400:
        logger.warning(
            "inline skill resolve returned HTTP %s (failure-open)",
            resp.status_code,
            extra={"skill_name": name},
        )
        return None
    try:
        payload = resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "inline skill resolve returned non-JSON (failure-open): %s",
            exc,
            extra={"skill_name": name},
        )
        return None

    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list) or not items:
        # Skill not installed / not resolvable for this user — silently skip.
        return None
    item = items[0]
    if not isinstance(item, dict):
        return None
    resolved_name = str(item.get("name") or "").strip()
    if not resolved_name:
        return None
    return SkillCatalogEntry(
        name=resolved_name,
        version=str(item.get("version") or ""),
        sha256=str(item.get("sha256") or ""),
        description=str(item.get("description") or "").strip(),
    )


async def resolve_skills(
    names: list[str] | None,
    *,
    skill_store_url: str,
    service_scopes: str,
    tenant_id: str,
    user_id: str,
    timeout: float = 5.0,
) -> list[SkillCatalogEntry]:
    """Resolve skill ``names`` into a catalog of ``SkillCatalogEntry`` (failure-open).

    Returns an entry per name that resolved successfully; a name that fails to
    resolve (skill-store error, not installed, malformed row) is skipped with a
    warning and never fails the run.  Order follows ``names`` with duplicates
    collapsed.  An empty / ``None`` ``names`` or an unset ``skill_store_url``
    yields an empty catalog (no skill loop for this run).
    """
    if not names or not skill_store_url:
        return []

    seen: set[str] = set()
    ordered: list[str] = []
    for raw in names:
        name = str(raw or "").strip()
        if name and name not in seen:
            seen.add(name)
            ordered.append(name)
    if not ordered:
        return []

    entries: list[SkillCatalogEntry] = []
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            for name in ordered:
                entry = await _resolve_one(
                    client=client,
                    skill_store_url=skill_store_url,
                    service_scopes=service_scopes,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    name=name,
                )
                if entry is not None:
                    entries.append(entry)
    except Exception as exc:  # noqa: BLE001 — never fail the run over skill resolution
        logger.warning("inline skill resolve batch failed (failure-open): %s", exc)
    return entries


def _cache_get(key: tuple[str, str]) -> str | None:
    val = _file_cache.get(key)
    if val is not None:
        _file_cache.move_to_end(key)
    return val


def _cache_put(key: tuple[str, str], value: str) -> None:
    _file_cache[key] = value
    _file_cache.move_to_end(key)
    while len(_file_cache) > _FILE_CACHE_MAX_ENTRIES:
        _file_cache.popitem(last=False)


def _truncate_body(content: str) -> str:
    """Truncate ``content`` to :data:`_SKILL_FILE_MAX_BYTES` UTF-8 bytes, with a notice."""
    encoded = content.encode("utf-8")
    if len(encoded) <= _SKILL_FILE_MAX_BYTES:
        return content
    clipped = encoded[:_SKILL_FILE_MAX_BYTES].decode("utf-8", errors="ignore")
    return clipped + _TRUNCATION_NOTICE


async def fetch_skill_file(
    name: str,
    *,
    skill_store_url: str,
    service_scopes: str,
    tenant_id: str,
    user_id: str,
    sha256: str = "",
    path: str = "SKILL.md",
    timeout: float = 5.0,
) -> str:
    """Fetch one skill package file's UTF-8 body (content-addressed LRU cached).

    Calls ``GET /internal/skills/{name}/file?path=<path>`` → ``{content, ...}``.
    The body is capped at 64KB (a truncation notice is appended past that).

    ``sha256`` is the resolved skill's content hash; when provided it keys the
    LRU cache alongside ``path`` so repeat reads are free and never stale.  When
    absent (e.g. the model asked for a skill outside the catalog) the fetch still
    works but is not cached.

    Failure-open: this NEVER raises.  On any error it returns a short Chinese
    error string suitable to hand back as the tool result — the run continues and
    the model can react to (or ignore) the failure.
    """
    read_path = (path or "SKILL.md").strip() or "SKILL.md"

    cache_key: tuple[str, str] | None = None
    if sha256:
        cache_key = (sha256, read_path)
        cached = _cache_get(cache_key)
        if cached is not None:
            return cached

    if not skill_store_url:
        return f"（无法读取技能「{name}」的 {read_path}：技能服务未配置）"

    url = f"{skill_store_url.rstrip('/')}/internal/skills/{name}/file"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(
                url,
                params={"tenant_id": tenant_id, "user_id": user_id, "path": read_path},
                headers=_skill_store_headers(
                    service_scopes=service_scopes, tenant_id=tenant_id, user_id=user_id
                ),
            )
    except httpx.HTTPError as exc:
        logger.warning(
            "inline skill file fetch failed (failure-open): %s",
            exc,
            extra={"skill_name": name, "path": read_path},
        )
        return f"（读取技能「{name}」的 {read_path} 失败：服务不可达）"

    if resp.status_code == 404:
        return f"（技能「{name}」或文件 {read_path} 不存在）"
    if resp.status_code == 400:
        return f"（读取技能「{name}」的路径 {read_path} 非法，已拒绝）"
    if resp.status_code == 415:
        return f"（技能「{name}」的文件 {read_path} 不是 UTF-8 文本，无法读取）"
    if resp.status_code == 413:
        return f"（技能「{name}」的文件 {read_path} 过大，无法读取）"
    if resp.status_code >= 400:
        logger.warning(
            "inline skill file fetch returned HTTP %s (failure-open)",
            resp.status_code,
            extra={"skill_name": name, "path": read_path},
        )
        return f"（读取技能「{name}」的 {read_path} 失败：HTTP {resp.status_code}）"

    try:
        payload = resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "inline skill file fetch returned non-JSON (failure-open): %s",
            exc,
            extra={"skill_name": name, "path": read_path},
        )
        return f"（读取技能「{name}」的 {read_path} 失败：响应格式错误）"

    content = str((payload or {}).get("content") or "")
    body = _truncate_body(content)

    # Cache under the resolved sha when the caller carried one, OR the sha the
    # file endpoint echoed back (content-addressed, so still never stale).
    resp_sha = str((payload or {}).get("sha256") or "")
    if cache_key is not None:
        _cache_put(cache_key, body)
    elif resp_sha:
        _cache_put((resp_sha, read_path), body)

    return body
