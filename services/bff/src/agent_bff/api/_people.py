"""Display names for the user ids other services return.

Services record who did something as a user id (``usr_…``).  To show people
rather than ids, the BFF looks each id up in foundation-identity
(``GET /users/{user_id}``) with the caller's own credentials, so only users of
the caller's tenant resolve, and passes on the display name alone — never the
rest of the identity profile (email, roles, clearance).

Lookups are best-effort: an id identity does not know (an erased author's
placeholder, a deleted user) or an identity outage leaves the name out and the
page falls back to a neutral label.  The whole lookup is bounded by
``_BUDGET_SECONDS`` so a slow identity never holds up the page, and a failed
lookup is remembered briefly so an outage is not retried on every request.

Names are cached per tenant for ``_TTL_SECONDS`` (a catalogue lists many apps
by few authors); a user's own rename clears their entry at once.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import OrderedDict
from collections.abc import Iterable

import httpx

from agent_bff.api._http import foundation_headers

logger = logging.getLogger(__name__)

# The ids identity's ``GET /users/{user_id}`` accepts; anything else is never
# looked up.
_USER_ID_RE = re.compile(r"^usr_[0-9A-Z]{26}$")
_TTL_SECONDS = 300.0
_FAILURE_TTL_SECONDS = 30.0
_MAX_ENTRIES = 4096
_CONCURRENCY = 8
_TIMEOUT_SECONDS = 3.0
_BUDGET_SECONDS = 2.5

# (tenant_id, user_id) -> (expires_at, display name, or None when there is none)
_cache: OrderedDict[tuple[str, str], tuple[float, str | None]] = OrderedDict()


def forget(tenant_id: str, user_id: str) -> None:
    """Drop a user's cached name (they just renamed themselves)."""
    _cache.pop((tenant_id, user_id), None)


def clear() -> None:
    """Drop every cached name (tests)."""
    _cache.clear()


def _cached(key: tuple[str, str]) -> tuple[bool, str | None]:
    hit = _cache.get(key)
    if hit is None:
        return False, None
    expires_at, name = hit
    if expires_at <= time.monotonic():
        del _cache[key]
        return False, None
    return True, name


def _remember(key: tuple[str, str], name: str | None, ttl: float = _TTL_SECONDS) -> None:
    _cache[key] = (time.monotonic() + ttl, name)
    _cache.move_to_end(key)
    while len(_cache) > _MAX_ENTRIES:
        _cache.popitem(last=False)


def _display_name(resp: httpx.Response) -> str | None:
    try:
        body = resp.json()
    except ValueError:
        return None
    name = body.get("display_name") if isinstance(body, dict) else None
    return (name.strip() or None) if isinstance(name, str) else None


async def display_names(
    identity_url: str,
    auth: tuple[str, str, str],
    user_ids: Iterable[object],
) -> dict[str, str]:
    """Map each resolvable id in ``user_ids`` to its display name.

    ``auth`` is the caller's ``(tenant_id, user_id, access_token)``.  Ids that
    are not user ids, unknown to identity, or not resolved within the budget
    are simply absent from the result.
    """
    tenant_id, caller_id, access_token = auth
    names: dict[str, str] = {}
    missing: list[str] = []
    for uid in {u for u in user_ids if isinstance(u, str) and _USER_ID_RE.match(u)}:
        found, name = _cached((tenant_id, uid))
        if not found:
            missing.append(uid)
        elif name:
            names[uid] = name
    if not missing:
        return names

    headers = foundation_headers(tenant_id, caller_id, access_token)
    gate = asyncio.Semaphore(_CONCURRENCY)

    async def lookup(client: httpx.AsyncClient, uid: str) -> None:
        key = (tenant_id, uid)
        async with gate:
            try:
                resp = await client.get(f"{identity_url}/users/{uid}", headers=headers)
            except httpx.HTTPError as exc:
                logger.warning("display name lookup failed: %s", exc)
                _remember(key, None, _FAILURE_TTL_SECONDS)
                return
        if resp.status_code == 404:
            _remember(key, None)
            return
        if resp.status_code != 200:
            logger.warning("display name lookup returned HTTP %s", resp.status_code)
            _remember(key, None, _FAILURE_TTL_SECONDS)
            return
        name = _display_name(resp)
        _remember(key, name)
        if name:
            names[uid] = name

    async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
        tasks = [asyncio.ensure_future(lookup(client, uid)) for uid in missing]
        _done, pending = await asyncio.wait(tasks, timeout=_BUDGET_SECONDS)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
    return names
