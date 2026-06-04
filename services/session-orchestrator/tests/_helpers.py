"""Constants + helpers shared by test modules.

Lives in its own module (rather than conftest.py) so test files can
``from tests._helpers import ...`` — pytest auto-loads conftest into a
private namespace that isn't importable from other test files.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import httpx

TEST_TENANT = "tnt_01J5TEST00000000000000TNT1"
TEST_USER = "usr_01J5TEST0000000000000USER1"


def make_scalar_result(value: Any) -> MagicMock:
    """Build a fake SQLAlchemy Result whose scalar_one_or_none / scalars match."""
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=value)
    scalars = MagicMock()
    scalars.all = MagicMock(return_value=value if isinstance(value, list) else [])
    result.scalars = MagicMock(return_value=scalars)
    return result


class AuthzAwareClient:
    """A base async httpx.AsyncClient stub that auto-allows authz /check calls.

    Subclasses can override ``post`` and ``get`` to handle application-specific
    URLs while this base handles authz transparently.
    """

    async def __aenter__(self) -> AuthzAwareClient:
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    def _authz_allowed_resp(self) -> httpx.Response:
        return httpx.Response(200, json={"allowed": True})

    async def post(self, url: str, **kw: Any) -> httpx.Response:
        if "/check" in url and "authz" in url:
            return self._authz_allowed_resp()
        return httpx.Response(200, json={})

    async def get(self, url: str, **kw: Any) -> httpx.Response:
        return httpx.Response(200, json={})
