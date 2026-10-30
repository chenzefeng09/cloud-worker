"""The effective DLP policy is in the log as soon as the service starts.

Before, it was logged at the first upload or download, so a deployment running
with DLP switched off or failing open said nothing about it until a user hit
that path.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest

from agent_session_orchestrator.api import dlp_guard
from agent_session_orchestrator.api.dlp_guard import configured_failure_mode
from agent_session_orchestrator.config import Settings

_LOGGER = "agent_session_orchestrator.api.dlp_guard"
_ENV = "SESSION_ORCHESTRATOR_DLP_FAILURE_MODE"
_DLP = "http://dlp.test/api/dlp/v1"


@pytest.fixture(autouse=True)
def _nothing_logged_yet(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.delenv(_ENV, raising=False)
    configured_failure_mode.cache_clear()
    dlp_guard._logged_policies.clear()
    yield
    configured_failure_mode.cache_clear()
    dlp_guard._logged_policies.clear()


@pytest.mark.parametrize(
    ("dlp_url", "mode", "level", "fragment"),
    [
        (_DLP, None, logging.INFO, "DLP_FAILURE_MODE=closed"),
        (_DLP, "open", logging.WARNING, "UNCHECKED"),
        ("", None, logging.WARNING, "DLP disabled"),
    ],
)
def test_startup_logs_the_effective_policy(
    dlp_url: str,
    mode: str | None,
    level: int,
    fragment: str,
    settings: Settings,
    request: pytest.FixtureRequest,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings.foundation_dlp_url = dlp_url
    if mode is not None:
        monkeypatch.setenv(_ENV, mode)

    with caplog.at_level(logging.INFO, logger=_LOGGER):
        request.getfixturevalue("app_client")  # starts the app (runs its lifespan)

    lines = [r for r in caplog.records if r.name == _LOGGER]
    assert [r.levelno for r in lines] == [level]
    assert fragment in lines[0].getMessage()
