"""JWT claim extraction helpers for the BFF.

The BFF receives Keycloak access tokens from the foundation identity login
flow.  Those tokens are already trusted — they were freshly exchanged from
Keycloak by identity and handed back over a server-to-server call — so the BFF
only needs to *read* claims out of them, not re-verify the signature.  Doing a
full JWKS-verified decode here would add a network dependency (and a trust
chain) the BFF deliberately stays out of: downstream foundation services are
the ones that JWKS-verify the bearer token.
"""

from __future__ import annotations

import logging

from jose import jwt

logger = logging.getLogger(__name__)

# The clearance levels Keycloak emits via the ``clearance`` protocol mapper.
_VALID_CLEARANCES = frozenset({"L1", "L2", "L3", "L4", "L5"})

# Fallback when the claim is missing, malformed, or the token can't be parsed.
# L2 is the baseline clearance for an ordinary authenticated user.
_DEFAULT_CLEARANCE = "L2"


def clearance_from_token(access_token: str) -> str:
    """Decode the clearance claim from a Keycloak access token.

    Returns 'L1'-'L5'; falls back to 'L2' on any decode failure / missing /
    invalid claim.

    This reads the claim *without* verifying the signature
    (``jwt.get_unverified_claims``): the token is already trusted by the time
    it reaches the BFF (see module docstring), and avoiding signature
    verification keeps the BFF out of the JWKS trust chain.
    """
    if not access_token:
        return _DEFAULT_CLEARANCE
    try:
        claims = jwt.get_unverified_claims(access_token)
    except Exception:  # noqa: BLE001 — any parse failure falls back to default
        logger.warning("clearance_from_token: failed to decode access token")
        return _DEFAULT_CLEARANCE
    clearance = claims.get("clearance")
    if clearance in _VALID_CLEARANCES:
        return clearance
    return _DEFAULT_CLEARANCE
