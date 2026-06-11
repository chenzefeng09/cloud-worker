"""Core feature-flag client — fetch, cache, evaluate."""

from __future__ import annotations

import logging
import time

import httpx

logger = logging.getLogger(__name__)

_FF_TIMEOUT = 2.0


class FeatureFlagClient:
    """Thin async client for the foundation feature-flag service.

    Fetches the full flag list per tenant, caches it with a short TTL,
    and exposes a simple ``is_enabled(key)`` check.  Fail-closed: any
    network / parse error returns ``False``.
    """

    def __init__(self, base_url: str, cache_ttl: float = 30.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._cache_ttl = cache_ttl
        # tenant_id → (expires_at_monotonic, {flag_key: enabled})
        self._cache: dict[str, tuple[float, dict[str, bool]]] = {}

    async def _fetch(self, tenant_id: str) -> dict[str, bool]:
        async with httpx.AsyncClient(timeout=_FF_TIMEOUT) as client:
            resp = await client.get(
                f"{self._base_url}/flags",
                headers={"X-Tenant-Id": tenant_id},
            )
            resp.raise_for_status()
        flags: dict[str, bool] = {}
        for item in resp.json():
            key = item["key"]
            enabled = bool(item.get("enabled")) and not bool(
                item.get("kill_switch_active")
            )
            flags[key] = enabled
        return flags

    async def is_enabled(self, key: str, tenant_id: str) -> bool:
        """Return whether *key* is enabled for *tenant_id* (fail-closed)."""
        now = time.monotonic()
        cached = self._cache.get(tenant_id)
        if cached and cached[0] > now:
            return cached[1].get(key, False)

        try:
            flags = await self._fetch(tenant_id)
        except Exception:
            logger.debug(
                "feature-flag fetch failed for tenant=%s", tenant_id, exc_info=True
            )
            return False

        self._cache[tenant_id] = (now + self._cache_ttl, flags)
        return flags.get(key, False)

    def invalidate(self, tenant_id: str | None = None) -> None:
        """Clear cached flags for one tenant (or all)."""
        if tenant_id is None:
            self._cache.clear()
        else:
            self._cache.pop(tenant_id, None)


# ─── Module-level singleton ─────────────────────────────────────────────────

_client: FeatureFlagClient | None = None


def init_feature_flags(
    base_url: str, cache_ttl: float = 30.0
) -> FeatureFlagClient:
    """Initialise the module-level client.  Call once at app startup."""
    global _client
    _client = FeatureFlagClient(base_url, cache_ttl)
    return _client


def get_ff_client() -> FeatureFlagClient:
    """Return the module-level client (raises if not initialised)."""
    if _client is None:
        raise RuntimeError(
            "FeatureFlagClient not initialised — call init_feature_flags() at startup"
        )
    return _client
