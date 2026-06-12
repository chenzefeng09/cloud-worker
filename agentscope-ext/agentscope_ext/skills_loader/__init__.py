"""Surface mounted skill packages via *progressive disclosure*.

Skills are no longer downloaded inside the sandbox.  The scheduler
materialises each task's pinned skill version on the host and bind-mounts
it read-only at ``/home/skills/<name>/`` (with ``<name>`` already
whitelisted to ``^[A-Za-z0-9._-]{1,64}$`` by the cache).  The mount root
is configurable via ``SANDBOX_SKILLS_MOUNT_ROOT`` for tests/dev.

This module's job is:

1. Iterate ``task["skills"]``.
2. For each name, look under ``<mount_root>/<name>/`` for a ``SKILL.md``
   in the root layout, or in a single-level subdirectory layout
   (``<sub>/SKILL.md``), mirroring the skill-store's ``skill_package`` rules.
3. Parse the YAML frontmatter to recover the ``description`` and return
   ``{name, description, skill_md_abspath}`` so the worker can advertise
   the skill in the system prompt — the agent itself reads the file with
   the ``Read`` tool on demand.

Everything is failure-open: missing mounts, missing/malformed SKILL.md,
unexpected exceptions — log a warning and skip.  The worker must never
crash because of a broken skill.

Because skills produce no tools, this package does **not** wrap
``build_toolkit``.  :func:`install` stays as an idempotent no-op so
``bootstrap.enable_from_env`` can still list ``skills_loader`` in
``RUNTIME_EXTENSIONS`` without error.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Iterable, Mapping

import yaml

_LOG = logging.getLogger(__name__)

_DEFAULT_MOUNT_ROOT = "/home/skills"
_SKILL_MD_FILENAME = "SKILL.md"


def install() -> None:
    """No-op extension hook.

    Retained so ``RUNTIME_EXTENSIONS=...,skills_loader,...`` keeps working
    through ``bootstrap.enable_from_env`` (which requires a callable
    ``install``).  Skills are surfaced via the system prompt, not by
    wrapping ``build_toolkit``.
    """
    return None


def collect_skill_prompts(
    task: Mapping[str, Any], tenant_id: str, env: Mapping[str, str]
) -> list[dict[str, str]]:
    """Scan the mount root and return one prompt entry per mounted skill.

    Returns a list of ``{name, description, skill_md_abspath}`` dicts — one
    per skill whose mount was found and whose ``SKILL.md`` frontmatter
    parsed.  Skills that aren't mounted (scheduler-side failure-open) or
    that have a broken/missing ``SKILL.md`` are logged and skipped.

    The ``tenant_id`` argument is accepted for signature compatibility with
    the previous network-based implementation; it is not used here because
    the scheduler already scoped the mount to this task.
    """
    try:
        skill_names = _coerce_names(task.get("skills"))
        if not skill_names:
            return []

        mount_root = Path(env.get("SANDBOX_SKILLS_MOUNT_ROOT") or _DEFAULT_MOUNT_ROOT)
        if not mount_root.is_dir():
            _LOG.warning(
                "skills_loader: mount root %s does not exist; no skills available",
                mount_root,
            )
            return []

        prompts: list[dict[str, str]] = []
        for name in skill_names:
            entry = _resolve_mounted_skill(mount_root, name)
            if entry is not None:
                prompts.append(entry)
        return prompts
    except Exception as exc:  # noqa: BLE001 -- failure-open per contract
        _LOG.warning("skills_loader: collect_skill_prompts failed: %r", exc)
        return []


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _resolve_mounted_skill(mount_root: Path, name: str) -> dict[str, str] | None:
    """Find SKILL.md for a single mounted skill and parse its frontmatter.

    Layout rules (mirrors ``skill_package`` upload validation):
    * ``<mount_root>/<name>/SKILL.md`` (root layout), OR
    * ``<mount_root>/<name>/<sub>/SKILL.md`` (single subdirectory layout).

    Anything deeper, missing, ambiguous, or malformed → warning + ``None``.
    """
    skill_dir = mount_root / name
    if not skill_dir.is_dir():
        _LOG.warning("skills_loader: skill %r not mounted at %s; skipping", name, skill_dir)
        return None

    skill_md = _find_skill_md(skill_dir)
    if skill_md is None:
        _LOG.warning(
            "skills_loader: skill %r mounted but SKILL.md not found under %s; skipping",
            name,
            skill_dir,
        )
        return None

    try:
        text = skill_md.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        _LOG.warning("skills_loader: cannot read %s: %r; skipping skill %r", skill_md, exc, name)
        return None

    parsed = _parse_frontmatter(text)
    if parsed is None:
        _LOG.warning(
            "skills_loader: skill %r at %s has malformed/missing frontmatter; skipping",
            name,
            skill_md,
        )
        return None

    description = parsed.get("description")
    if not isinstance(description, str) or not description.strip():
        _LOG.warning(
            "skills_loader: skill %r has empty/missing description in frontmatter; "
            "advertising with empty description",
            name,
        )
        description = ""
    else:
        description = description.strip()

    return {
        "name": name,
        "description": description,
        "skill_md_abspath": str(skill_md.resolve()),
    }


def _find_skill_md(skill_dir: Path) -> Path | None:
    """Locate the SKILL.md inside a mounted skill directory.

    Accepts the same two layouts the skill-store enforces at upload time:
    root, or a single-level subdirectory.  Multiple SKILL.md files or
    deeper nesting count as malformed and yield ``None``.
    """
    root_candidate = skill_dir / _SKILL_MD_FILENAME
    if root_candidate.is_file():
        return root_candidate

    # Single-subdirectory layout: look one level deep.
    matches: list[Path] = []
    try:
        children = list(skill_dir.iterdir())
    except OSError:
        return None
    for child in children:
        if not child.is_dir():
            continue
        nested = child / _SKILL_MD_FILENAME
        if nested.is_file():
            matches.append(nested)
    if len(matches) == 1:
        return matches[0]
    return None


def _parse_frontmatter(text: str) -> dict[str, Any] | None:
    """Parse the leading ``---\\n…\\n---`` block as YAML.

    Returns the parsed mapping, or ``None`` if the document is missing
    the fences, has no closing fence, the YAML is malformed, or the
    payload isn't a mapping.  Never raises.
    """
    # Allow a leading BOM/newline, mirroring skill_package._parse_frontmatter.
    stripped = text.lstrip("﻿")
    if not stripped.startswith("---"):
        return None

    lines = stripped.splitlines()
    closing_idx: int | None = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            closing_idx = i
            break
    if closing_idx is None:
        return None

    frontmatter_text = "\n".join(lines[1:closing_idx])
    try:
        data = yaml.safe_load(frontmatter_text)
    except yaml.YAMLError:
        return None
    if not isinstance(data, dict):
        return None
    return data


def _coerce_names(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, Iterable):
        return [str(v) for v in value if v]
    return []


__all__ = ["install", "collect_skill_prompts"]
