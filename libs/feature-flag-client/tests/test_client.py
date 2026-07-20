from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

from feature_flag_client import FeatureFlagClient


def test_is_enabled_default_true_for_missing_flag() -> None:
    client = FeatureFlagClient("http://flags")
    client._fetch = AsyncMock(return_value={})  # type: ignore[method-assign]

    assert asyncio.get_event_loop().run_until_complete(
        client.is_enabled("optional.feature", "tenant", default=True)
    )


def test_is_enabled_explicit_false_overrides_default_true() -> None:
    client = FeatureFlagClient("http://flags")
    client._fetch = AsyncMock(  # type: ignore[method-assign]
        return_value={"optional.feature": False}
    )

    assert not asyncio.get_event_loop().run_until_complete(
        client.is_enabled("optional.feature", "tenant", default=True)
    )


def test_is_enabled_uses_default_on_fetch_error() -> None:
    client = FeatureFlagClient("http://flags")
    client._fetch = AsyncMock(side_effect=RuntimeError("offline"))  # type: ignore[method-assign]

    loop = asyncio.get_event_loop()
    assert loop.run_until_complete(
        client.is_enabled("optional.feature", "tenant", default=True)
    )
    assert not loop.run_until_complete(
        client.is_enabled("required.feature", "other-tenant")
    )
