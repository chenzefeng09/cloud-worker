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

Token streaming
---------------
``agent:run:{run_id}:tokens`` → Redis Stream

Each entry has two fields:
  ``type``  ``delta`` | ``end``
  ``text``  the delta string (empty for ``end`` entries)

The stream is capped at 10 000 entries (``MAXLEN ~ 10000``) and expires
after ``STATE_TTL_SECONDS`` seconds (same TTL as the state key).
Consumers should read until they see a ``type=end`` entry.
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


def _make_token_stream_key(run_id: str) -> str:
    return f"{_KEY_PREFIX}:{run_id}:tokens"


# Maximum number of entries to retain in the token stream.
# XADD uses approximate trimming (MAXLEN ~) which keeps Redis CPU cheap.
_TOKEN_STREAM_MAXLEN = 10_000


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
        self._token_stream_key = _make_token_stream_key(run_id)
        self._client: Any | None = None
        self._step = 0
        # Tracks whether we have already set EXPIRE on the token stream key.
        self._token_stream_expire_set: bool = False

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

    def push_token(self, delta: str) -> None:
        """Append one token delta to the Redis Stream ``agent:run:{run_id}:tokens``.

        Contract (consumed by the orchestrator SSE bridge):
        * Stream key : ``agent:run:{run_id}:tokens``
        * Entry fields: ``type=delta``, ``text=<delta string>``
        * Stream is capped at ``_TOKEN_STREAM_MAXLEN`` entries (approximate).
        * TTL is set to ``STATE_TTL_SECONDS`` on the first push.

        Failure-open: any Redis error is logged as WARNING and swallowed.
        The agent run must never crash because of a streaming failure.
        """
        if self._client is None:
            return
        try:
            self._client.xadd(
                self._token_stream_key,
                {"type": "delta", "text": delta},
                maxlen=_TOKEN_STREAM_MAXLEN,
                approximate=True,
            )
            # Set TTL on the first successful write so the stream does not
            # accumulate in Redis indefinitely if the consumer never reads it.
            if not self._token_stream_expire_set:
                self._client.expire(self._token_stream_key, STATE_TTL_SECONDS)
                self._token_stream_expire_set = True
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Redis token push failed for run %s: %s",
                self._run_id,
                exc,
            )

    def push_token_end(self) -> None:
        """Append a terminal ``type=end`` entry to the token stream.

        Consumers poll the stream and stop when they see this entry.
        Failure-open: errors are logged and swallowed.
        """
        if self._client is None:
            return
        try:
            self._client.xadd(
                self._token_stream_key,
                {"type": "end", "text": ""},
                maxlen=_TOKEN_STREAM_MAXLEN,
                approximate=True,
            )
            # Ensure TTL is set even if no deltas were pushed (e.g. empty reply).
            if not self._token_stream_expire_set:
                self._client.expire(self._token_stream_key, STATE_TTL_SECONDS)
                self._token_stream_expire_set = True
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Redis token end push failed for run %s: %s",
                self._run_id,
                exc,
            )

    # Maximum result size (bytes) pushed to Redis — avoids bloating the stream
    # with huge tool outputs (e.g. file contents).  Excess is truncated.
    _TOOL_RESULT_MAX_BYTES = 8 * 1024  # 8 KB

    def push_tool_call(self, tool_call_id: str, name: str, args: str) -> None:
        """Append a ``type=tool_call`` entry to the shared token stream.

        The entry interleaves with ``type=delta`` / ``type=end`` entries in
        the same stream so the frontend can reconstruct the exact execution
        order.

        Contract::

            type=tool_call
            tool_call_id=<id>
            name=<tool name, e.g. "Read">
            args=<complete JSON args string>

        Failure-open: any Redis error is logged as WARNING and swallowed.
        """
        if self._client is None:
            return
        try:
            self._client.xadd(
                self._token_stream_key,
                {
                    "type": "tool_call",
                    "tool_call_id": tool_call_id,
                    "name": name,
                    "args": args,
                },
                maxlen=_TOKEN_STREAM_MAXLEN,
                approximate=True,
            )
            if not self._token_stream_expire_set:
                self._client.expire(self._token_stream_key, STATE_TTL_SECONDS)
                self._token_stream_expire_set = True
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Redis tool_call push failed for run %s: %s",
                self._run_id,
                exc,
            )

    def push_tool_result(
        self,
        tool_call_id: str,
        name: str,
        result: str,
        state: str,
    ) -> None:
        """Append a ``type=tool_result`` entry to the shared token stream.

        Large results are truncated to ``_TOOL_RESULT_MAX_BYTES`` bytes to
        avoid bloating Redis / SSE payloads.

        Contract::

            type=tool_result
            tool_call_id=<id>
            name=<tool name>
            result=<tool output text, possibly truncated>
            state=<"success" | ...>

        Failure-open: any Redis error is logged as WARNING and swallowed.
        """
        if self._client is None:
            return
        try:
            # Truncate to avoid oversized stream entries.
            result_bytes = result.encode("utf-8", errors="replace")
            if len(result_bytes) > self._TOOL_RESULT_MAX_BYTES:
                result = (
                    result_bytes[: self._TOOL_RESULT_MAX_BYTES].decode(
                        "utf-8", errors="replace"
                    )
                    + "…[truncated]"
                )
            self._client.xadd(
                self._token_stream_key,
                {
                    "type": "tool_result",
                    "tool_call_id": tool_call_id,
                    "name": name,
                    "result": result,
                    "state": state,
                },
                maxlen=_TOKEN_STREAM_MAXLEN,
                approximate=True,
            )
            if not self._token_stream_expire_set:
                self._client.expire(self._token_stream_key, STATE_TTL_SECONDS)
                self._token_stream_expire_set = True
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Redis tool_result push failed for run %s: %s",
                self._run_id,
                exc,
            )

    def push_thinking(self, text: str) -> None:
        """Append a ``type=thinking`` entry to the shared token stream.

        Contract::

            type=thinking
            text=<thinking text>

        Failure-open: any Redis error is logged as WARNING and swallowed.
        """
        if self._client is None:
            return
        try:
            self._client.xadd(
                self._token_stream_key,
                {"type": "thinking", "text": text},
                maxlen=_TOKEN_STREAM_MAXLEN,
                approximate=True,
            )
            if not self._token_stream_expire_set:
                self._client.expire(self._token_stream_key, STATE_TTL_SECONDS)
                self._token_stream_expire_set = True
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Redis thinking push failed for run %s: %s",
                self._run_id,
                exc,
            )

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
