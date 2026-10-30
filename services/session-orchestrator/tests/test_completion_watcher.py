"""The completion watcher settles finished runs nobody is watching.

A board task or a scheduled run has no open stream to reconcile it, and the
stale-run sweeper only probes runs older than two minutes.  The watcher reads
the newest entry of every live run's token stream and reconciles the runs
whose worker wrote its final ``end`` marker; one the scheduler still reports
live is probed again after a growing delay.

``_watch_once`` is driven directly with the scan, the Redis read and the
per-run reconcile replaced, so no Postgres or Redis is needed.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

import agent_session_orchestrator.services.reconcile_sweeper as watcher
from agent_session_orchestrator.config import Settings
from tests._helpers import TEST_TENANT, TEST_USER


def _row(run_id: str) -> str:
    return "\x1f".join([TEST_TENANT, run_id, f"ses_{run_id}", TEST_USER])


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {
        "live": [],          # run ids the scan returns
        "ended": set(),      # run ids whose stream ends with ``end``
        "after": {},         # run id -> status the reconcile leaves
        "sql": [],
    }

    async def _scan(settings: Settings, sql: str, params: dict | None = None) -> list:
        state["sql"].append(sql)
        return [_row(run_id) for run_id in state["live"]]

    async def _ended(redis_url: str, run_ids: list[str]) -> set[str]:
        return {run_id for run_id in run_ids if run_id in state["ended"]}

    async def _reconcile(settings: Any, scanned: Any) -> str | None:
        return state["after"].get(scanned.run_id, "RUNNING")

    reconcile = AsyncMock(side_effect=_reconcile)
    clock = _Clock()
    monkeypatch.setattr(watcher, "privileged_scalars", _scan)
    monkeypatch.setattr(watcher, "_ended_runs", _ended)
    monkeypatch.setattr(watcher, "_reconcile_scanned_run", reconcile)
    monkeypatch.setattr(watcher.time, "monotonic", clock.monotonic)

    app = MagicMock()
    app.state.settings = Settings(env="test", otel_enabled=False, redis_url="redis://fake")
    return {"state": state, "reconcile": reconcile, "clock": clock, "app": app}


def _reconciled(reconcile: AsyncMock) -> list[str]:
    return [call.args[1].run_id for call in reconcile.await_args_list]


@pytest.mark.asyncio
async def test_only_runs_whose_worker_finished_are_reconciled(harness: dict[str, Any]) -> None:
    state, reconcile = harness["state"], harness["reconcile"]
    state["live"] = ["run_done", "run_busy"]
    state["ended"] = {"run_done"}
    state["after"] = {"run_done": "COMPLETED"}

    await watcher._watch_once(harness["app"], {})

    assert _reconciled(reconcile) == ["run_done"]
    scanned = reconcile.await_args.args[1]
    assert (scanned.tenant_id, scanned.session_id, scanned.user_id) == (
        TEST_TENANT, "ses_run_done", TEST_USER,
    )
    # Live sandbox runs only, with the owner resolved like the sweeper's scan.
    (sql,) = state["sql"]
    assert "r.status IN ('RUNNING', 'PENDING')" in sql
    assert "r.sandbox_id IS NOT NULL" in sql
    assert "LEFT JOIN session_orchestrator.session" in sql


@pytest.mark.asyncio
async def test_an_ended_run_still_live_is_probed_with_a_growing_delay(
    harness: dict[str, Any],
) -> None:
    state, reconcile, clock = harness["state"], harness["reconcile"], harness["clock"]
    state["live"] = ["run_slow"]
    state["ended"] = {"run_slow"}
    retry_at: dict[str, tuple[float, float]] = {}

    await watcher._watch_once(harness["app"], retry_at)  # probe 1: still RUNNING
    first_delay = retry_at["run_slow"][1]
    assert first_delay == watcher.COMPLETION_POLL_SECONDS

    await watcher._watch_once(harness["app"], retry_at)  # too early: skipped
    assert len(reconcile.await_args_list) == 1

    clock.now += first_delay
    await watcher._watch_once(harness["app"], retry_at)  # probe 2
    assert len(reconcile.await_args_list) == 2
    assert retry_at["run_slow"][1] == first_delay * 2

    # The delay is capped.
    for _ in range(10):
        clock.now += watcher._ENDED_PROBE_MAX_DELAY_SECONDS
        await watcher._watch_once(harness["app"], retry_at)
    assert retry_at["run_slow"][1] == watcher._ENDED_PROBE_MAX_DELAY_SECONDS

    # Once settled it is forgotten.
    state["after"] = {"run_slow": "COMPLETED"}
    clock.now += watcher._ENDED_PROBE_MAX_DELAY_SECONDS
    await watcher._watch_once(harness["app"], retry_at)
    assert retry_at == {}


@pytest.mark.asyncio
async def test_runs_that_left_the_live_set_are_forgotten(harness: dict[str, Any]) -> None:
    state = harness["state"]
    retry_at = {"run_gone": (harness["clock"].now + 30, 30.0)}
    state["live"] = []

    await watcher._watch_once(harness["app"], retry_at)

    assert retry_at == {}
    harness["reconcile"].assert_not_awaited()


@pytest.mark.asyncio
async def test_a_failed_reconcile_is_retried_later_not_every_tick(
    harness: dict[str, Any],
) -> None:
    state, reconcile = harness["state"], harness["reconcile"]
    state["live"] = ["run_err"]
    state["ended"] = {"run_err"}
    state["after"] = {"run_err": None}
    retry_at: dict[str, tuple[float, float]] = {}

    await watcher._watch_once(harness["app"], retry_at)
    await watcher._watch_once(harness["app"], retry_at)

    assert len(reconcile.await_args_list) == 1
    assert "run_err" in retry_at


@pytest.mark.asyncio
async def test_the_watcher_needs_redis() -> None:
    app = MagicMock()
    app.state.settings = Settings(env="test", otel_enabled=False, redis_url=None)
    # Returns at once instead of looping.
    await watcher.run_completion_watcher(app)


# ── the Redis read ───────────────────────────────────────────────────────────


class _Pipeline:
    def __init__(self, streams: dict[str, list[tuple[str, dict[str, str]]]]) -> None:
        self._streams = streams
        self._keys: list[str] = []

    async def __aenter__(self) -> _Pipeline:
        return self

    async def __aexit__(self, *a: Any) -> None:
        return None

    def xrevrange(self, key: str, count: int) -> None:
        assert count == 1
        self._keys.append(key)

    async def execute(self) -> list[Any]:
        return [list(reversed(self._streams.get(key, [])))[:1] for key in self._keys]


@pytest.mark.asyncio
async def test_ended_runs_reads_the_newest_entry_of_each_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    streams = {
        "agent:run:run_done:tokens": [("1-0", {"type": "token"}), ("2-0", {"type": "end"})],
        "agent:run:run_paused:tokens": [("1-0", {"type": "end", "status": "paused"})],
        "agent:run:run_resumed:tokens": [
            ("1-0", {"type": "end", "status": "paused"}),
            ("2-0", {"type": "pending_resolved"}),
        ],
        "agent:run:run_busy:tokens": [("1-0", {"type": "token"})],
    }
    client = MagicMock()
    client.pipeline = MagicMock(return_value=_Pipeline(streams))
    client.aclose = AsyncMock()

    import redis.asyncio as aioredis

    monkeypatch.setattr(aioredis.Redis, "from_url", MagicMock(return_value=client))

    ended = await watcher._ended_runs(
        "redis://fake", ["run_done", "run_paused", "run_resumed", "run_busy", "run_expired"],
    )

    assert ended == {"run_done", "run_paused"}
    client.pipeline.assert_called_once_with(transaction=False)
    client.aclose.assert_awaited_once()
