"""Sensitivity / clearance ordering helpers.

The platform models data sensitivity and user clearance on the same ordered
scale ``L1 < L2 < L3 < L4 < L5`` (mirrors ``DlpSensitivity`` on the web/DLP
side).  Knowledge-ingest uses it for the *visibility* gate: a document is
visible to a caller only when the document sensitivity is ``<=`` the caller
clearance.

Everything is fail-closed:

* An unknown / missing clearance normalises to ``L1`` (the most restrictive).
* An unknown sensitivity ranks as ``L1`` for comparison purposes.
"""

from __future__ import annotations

# Ordered low → high.  Index == rank.
SENSITIVITY_LEVELS: tuple[str, ...] = ("L1", "L2", "L3", "L4", "L5")

DEFAULT_SENSITIVITY = "L1"
DEFAULT_CLEARANCE = "L1"


def is_valid_sensitivity(value: str | None) -> bool:
    return value in SENSITIVITY_LEVELS


def sensitivity_rank(value: str | None) -> int:
    """Rank of a level (0-based). Unknown / None → 0 (== L1, fail-closed)."""
    try:
        return SENSITIVITY_LEVELS.index(value)  # type: ignore[arg-type]
    except (ValueError, TypeError):
        return 0


def max_sensitivity(a: str, b: str) -> str:
    """Higher of two levels — used by the only-elevate move rule."""
    return a if sensitivity_rank(a) >= sensitivity_rank(b) else b


def normalize_clearance(raw: str | None) -> str:
    """Coerce a raw ``X-User-Clearance`` header value to a valid level.

    Missing or unrecognised → ``L1`` (conservative: caller sees least).
    """
    if raw is None:
        return DEFAULT_CLEARANCE
    value = raw.strip().upper()
    return value if value in SENSITIVITY_LEVELS else DEFAULT_CLEARANCE


def allowed_levels_for(clearance: str) -> list[str]:
    """Every sensitivity level a caller at ``clearance`` may see (<= clearance)."""
    ceiling = sensitivity_rank(normalize_clearance(clearance))
    return [lvl for lvl in SENSITIVITY_LEVELS if sensitivity_rank(lvl) <= ceiling]
