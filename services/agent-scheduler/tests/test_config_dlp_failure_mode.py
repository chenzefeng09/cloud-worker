"""The DLP failure mode forwarded to every sandbox as DLP_FAILURE_MODE.

A typo must stop the scheduler at startup instead of quietly leaving the worker
to fall back on its own default.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from agent_scheduler.config import Settings


def test_a_scan_that_cannot_complete_denies_by_default() -> None:
    assert Settings().dlp_failure_mode == "closed"


def test_the_mode_comes_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCHEDULER_DLP_FAILURE_MODE", "open")

    assert Settings().dlp_failure_mode == "open"


def test_an_unknown_mode_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(dlp_failure_mode="fail-open")
