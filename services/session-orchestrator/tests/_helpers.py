"""Constants + helpers shared by test modules.

Lives in its own module (rather than conftest.py) so test files can
``from tests._helpers import ...`` — pytest auto-loads conftest into a
private namespace that isn't importable from other test files.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

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
