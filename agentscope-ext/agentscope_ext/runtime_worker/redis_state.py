"""Thin failure-open Redis wrapper for agent run hot-state.

The worker writes a JSON state blob to Redis at key
``agent:run:{run_id}:state`` at every meaningful step boundary so that a
scheduler-side restart can detect the previous checkpoint and resume the
run without losing the agent's plan or progress counter.

Design choices
--------------
* **Failure-open**: if Redis is unreachable (no REDIS_URL env, network
  partition, wrong password) every operation logs a WARNING and returns
  gracefully.  The worker never crashes because of Redis.
* **Synchronous redis-py**: the sandbox entrypoint is already synchronous
  (asyncio.run wraps the one async call); adding an async Redis client
  would require a second event-loop and extra deps for no benefit.
* **Key TTL = sandbox_timeout * 3**: hot state should outlive the sandbox
  container long enough for the scheduler to read it on restart but not
  clog Redis permanently.  Default 30 min (3 × 600 s timeout).

Key schema
----------
``agent:run:{run_id}:state`` → JSON object:

  {
    "run_id":        str,
    "step":          int,   # monotonically increasing event counter
    "plan":          str,   # last observed ThinkEvent thought text (or "")
    "cursor":        str,   # last event_type seen (for resume context)
    "last_event_seq":int,   # same as step, kept for compat
    "status":        str,   # "RUNNING" | "COMPLETED" | "FAILED"
    "updated_step":  int    # echo of step (monotonic wall-clock substitute)
  }

Terminal states ("COMPLETED", "FAILED") keep the key for
``STATE_TTL_SECONDS`` so the scheduler can read the outcome even after
the container exits.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

# How long (seconds) to keep a state key in Redis after terminal state.
# 30 minutes is long enough for the scheduler to poll final status; Redis
# ``allkeys-lru`` evicts this automatically if memory gets tight.
STATE_TTL_SECONDS = 1800

# Key prefix — must stay stable across deploys (scheduler reads same key).
_KEY_PREFIX = "agent:run"


def _make_key(run_id: str) -> str:
    return f"{_KEY_PREFIX}:{run_id}:state"


class RunStateClient:
    """Failure-open Redis client scoped to a single agent run.

    Instantiate once in ``_run_real`` and pass the instance around.
    All public methods swallow exceptions and log warnings — callers never
    need to guard against Redis errors.

    Args:
        run_id: Stable identifier for this run (injected by scheduler as
            ``RUN_ID`` env var).
        redis_url: Full ``redis://[:password@]host:port/db`` URL.  When
            None (REDIS_URL unset) the client is a no-op.
    """

    def __init__(self, run_id: str, redis_url: str | None) -> None:
        self._run_id = run_id
        self._key = _make_key(run_id)
        self._client: Any | None = None
        self._step = 0

        if not redis_url:
            logger.debug("REDIS_URL unset; hot-state disabled for run %s", run_id)
            return
        if not run_id:
            logger.warning("RUN_ID unset; hot-state disabled (cannot build Redis key)")
            return

        try:
            import redis  # type: ignore[import-not-found]

            self._client = redis.Redis.from_url(
                redis_url,
                socket_connect_timeout=2,
                socket_timeout=2,
                decode_responses=True,
            )
            # Cheap ping to surface misconfiguration at startup rather than
            # silently failing on the first write mid-run.
            self._client.ping()
            logger.info("Hot-state Redis connected for run %s (key=%s)", run_id, self._key)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Redis unavailable for run %s; hot-state disabled: %s",
                run_id,
                exc,
            )
            self._client = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def read_checkpoint(self) -> dict | None:
        """Return the existing state blob, or None if absent/error."""
        if self._client is None:
            return None
        try:
            raw = self._client.get(self._key)
            if raw is None:
                return None
            return json.loads(raw)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Redis read failed for run %s: %s", self._run_id, exc)
            return None

    def write_step(self, *, plan: str = "", cursor: str = "", status: str = "RUNNING") -> None:
        """Upsert the state blob with the latest step counter + metadata."""
        if self._client is None:
            return
        self._step += 1
        state = {
            "run_id": self._run_id,
            "step": self._step,
            "plan": plan,
            "cursor": cursor,
            "last_event_seq": self._step,
            "status": status,
            "updated_step": self._step,
        }
        try:
            self._client.set(self._key, json.dumps(state), ex=STATE_TTL_SECONDS)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Redis write failed at step %d for run %s: %s",
                           self._step, self._run_id, exc)

    def mark_completed(self) -> None:
        """Set status=COMPLETED; keep key alive for TTL so scheduler can read it."""
        self._write_terminal("COMPLETED")

    def mark_failed(self) -> None:
        """Set status=FAILED; keep key alive for TTL."""
        self._write_terminal("FAILED")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _write_terminal(self, status: str) -> None:
        if self._client is None:
            return
        try:
            raw = self._client.get(self._key)
            if raw:
                state = json.loads(raw)
            else:
                state = {
                    "run_id": self._run_id,
                    "step": self._step,
                    "plan": "",
                    "cursor": "",
                    "last_event_seq": self._step,
                    "updated_step": self._step,
                }
            state["status"] = status
            self._client.set(self._key, json.dumps(state), ex=STATE_TTL_SECONDS)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Redis terminal write failed for run %s: %s", self._run_id, exc)


def from_env() -> RunStateClient:
    """Build a :class:`RunStateClient` from the current process environment.

    Reads ``RUN_ID`` and ``REDIS_URL`` env vars.  Returns a no-op client
    when either is unset — callers need not check.
    """
    run_id = os.environ.get("RUN_ID", "")
    redis_url = os.environ.get("REDIS_URL", "")
    return RunStateClient(run_id=run_id, redis_url=redis_url or None)
