"""Unit tests for clearance_from_token (api/_jwt.py)."""

from __future__ import annotations

import pytest
from jose import jwt

from agent_bff.api._jwt import clearance_from_token

_SECRET = "test-secret-not-verified-anyway"


def _token(claims: dict) -> str:
    return jwt.encode(claims, _SECRET, algorithm="HS256")


@pytest.mark.parametrize("level", ["L1", "L2", "L3", "L4", "L5"])
def test_valid_clearance_claim_returned(level: str) -> None:
    token = _token({"sub": "u1", "clearance": level})
    assert clearance_from_token(token) == level


def test_missing_claim_falls_back_to_l2() -> None:
    token = _token({"sub": "u1"})
    assert clearance_from_token(token) == "L2"


def test_invalid_claim_value_falls_back_to_l2() -> None:
    token = _token({"sub": "u1", "clearance": "L9"})
    assert clearance_from_token(token) == "L2"


def test_garbage_token_falls_back_to_l2() -> None:
    assert clearance_from_token("not-a-jwt") == "L2"


def test_empty_token_falls_back_to_l2() -> None:
    assert clearance_from_token("") == "L2"
