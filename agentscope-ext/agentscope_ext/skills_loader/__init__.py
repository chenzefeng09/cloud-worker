"""Materialise packaged Skills via *progressive disclosure*.

The worker no longer executes any user-uploaded skill code.  Instead each
skill package is downloaded, sha-verified and unpacked into
``/workspace/skills/<name>-<version>/``; the agent is then told the skill's
``name``/``description`` and the absolute path to its ``SKILL.md`` (injected
into the system prompt by ``runtime_worker.__main__``).  The agent reads the
full instructions on demand with the ``Read`` tool.

Because skills no longer produce tools, this package does **not** wrap the
``build_toolkit`` hook anymore (the MCP injector still owns that chain).
:func:`install` is kept as an idempotent no-op so ``bootstrap.enable_from_env``
can still list ``skills_loader`` in ``RUNTIME_EXTENSIONS`` without error.

The real entry point is :func:`collect_skill_prompts`, called directly by the
worker entrypoint after the toolkit is built.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable, Mapping

from . import loader

_LOG = logging.getLogger(__name__)


def install() -> None:
    """No-op extension hook.

    Retained so ``RUNTIME_EXTENSIONS=...,skills_loader,...`` keeps working
    through ``bootstrap.enable_from_env`` (which requires a callable
    ``install``).  Skills are now surfaced via the system prompt rather than
    by wrapping ``build_toolkit``.
    """
    return None


def collect_skill_prompts(
    task: Mapping[str, Any], tenant_id: str, env: Mapping[str, str]
) -> list[dict[str, str]]:
    """Resolve, download and unpack the task's skills for prompt injection.

    Returns a list of ``{name, description, skill_md_abspath}`` dicts — one per
    skill that was successfully unpacked into ``/workspace/skills/``.

    Failure-open throughout: a manifest-fetch error returns ``[]``; any single
    skill that fails to resolve/download/verify/unpack is logged and skipped
    so it can never crash the worker.
    """
    skill_names = _coerce_names(task.get("skills"))
    if not skill_names:
        return []

    try:
        # Prefer the dedicated SKILL_STORE_URL; fall back to FOUNDATION_API_URL
        # so the extension keeps working in legacy single-URL deployments.
        base_url = env.get("SKILL_STORE_URL") or env.get("FOUNDATION_API_URL", "")
        # FOUNDATION_SERVICE_SCOPES is injected by the scheduler into the
        # sandbox env.  In prod the API gateway sets X-Service-Scopes after
        # verifying the Bearer JWT; in dev (no gateway) this env var carries
        # the same value so the internal endpoint can authorise the call.
        service_scopes = env.get("FOUNDATION_SERVICE_SCOPES", "")
        token = env.get("FOUNDATION_API_TOKEN", "")
        skills = loader.fetch_skill_manifests(
            base_url=base_url,
            token=token,
            tenant_id=tenant_id,
            names=skill_names,
            service_scopes=service_scopes,
        )
    except Exception as exc:  # noqa: BLE001 -- failure-open
        _LOG.warning("skills_loader: manifest fetch failed: %r", exc)
        return []

    prompts: list[dict[str, str]] = []
    for skill in skills:
        try:
            target = loader.select_version(skill)
            if target is None:
                _LOG.warning(
                    "skills_loader: no usable version for %r",
                    skill.get("name") if isinstance(skill, Mapping) else skill,
                )
                continue
            result = loader.install_skill(target, token=token)
            if result is not None:
                prompts.append(result)
        except Exception as exc:  # noqa: BLE001 -- skip the bad one
            name = skill.get("name") if isinstance(skill, Mapping) else skill
            _LOG.warning("skills_loader: install failed for %r: %r", name, exc)
    return prompts


def _coerce_names(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, Iterable):
        return [str(v) for v in value if v]
    return []


__all__ = ["install", "collect_skill_prompts"]
