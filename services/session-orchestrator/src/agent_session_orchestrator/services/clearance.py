"""Clearance / sensitivity-level helpers.

Levels are L1..L5 with L1 < L2 < L3 < L4 < L5.  The server is authoritative on
the invariant ``file_level <= workspace_level <= user_clearance``; the helpers
here are the single source of truth for ranking so the API, service, and
run-context loader all agree on what "may this user touch this resource?" means.

* :func:`clearance_rank` - numeric rank for a level (L1=1 .. L5=5).  Raises
  ``ValueError`` for anything outside {L1..L5}, so callers also use it for
  format validation.
* :func:`level_allows` - boolean "may a caller with ``user_clearance`` access a
  resource tagged ``resource_level``?".  True iff ``rank(user) >= rank(resource)``.
"""

from __future__ import annotations

CLEARANCE_RANK: dict[str, int] = {"L1": 1, "L2": 2, "L3": 3, "L4": 4, "L5": 5}


def clearance_rank(level: str) -> int:
    """Numeric rank for a clearance/sensitivity level (L1=1 .. L5=5).

    Raises ``ValueError`` for anything outside {L1..L5} so this doubles as the
    format validator for the level string.
    """
    try:
        return CLEARANCE_RANK[level]
    except KeyError as exc:  # pragma: no cover - exercised via callers
        raise ValueError(f"invalid clearance level {level!r}") from exc


def level_allows(user_clearance: str, resource_level: str) -> bool:
    """Whether a caller with ``user_clearance`` may access ``resource_level``.

    True iff ``rank(user_clearance) >= rank(resource_level)`` - a caller may
    touch resources at or below their own clearance, never above.
    """
    return clearance_rank(user_clearance) >= clearance_rank(resource_level)
