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
    "status":        str,   # "RUNNING" | "COMPLETED" | "FAILED" | "PAUSED"
    "updated_step":  int    # echo of step (monotonic wall-clock substitute)
  }

Terminal states ("COMPLETED", "FAILED") keep the key for
``STATE_TTL_SECONDS`` so the scheduler can read the outcome even after
the container exits.  "PAUSED" state keeps the key for ``PAUSED_TTL_SECONDS``
(7 days) to allow the orchestrator to resume via a new sandbox container.

Token streaming
---------------
``agent:run:{run_id}:tokens`` → Redis Stream

Each entry has two fields:
  ``type``  ``delta`` | ``end`` | ``tool_call`` | ``tool_result`` |
            ``thinking`` | ``pending_confirmation`` | ``pending_question``
  ``text``  the delta string (empty for ``end`` entries)

The stream is capped at 10 000 entries (``MAXLEN ~ 10000``) and expires
after ``STATE_TTL_SECONDS`` seconds (same TTL as the state key).
Consumers should read until they see a ``type=end`` entry.  On pause,
no ``type=end`` entry is pushed (the reply is not finished); instead,
``pending_confirmation`` / ``pending_question`` entries announce the
pending tool calls and the orchestrator drives the resume handshake.

Pause blob
----------
``agent:run:{run_id}:paused`` → plain SET (JSON, TTL 7 days):

  {
    "kind":        "confirm" | "question",
    "reply_id":    str,
    "tool_calls":  [{"id": str, "name": str, "args": str}, ...],
    "question":    str | null,
    "agent_state": <AgentState.model_dump(mode="json")>,
    "paused_at":   ISO-8601 timestamp
  }

Written by :meth:`RunStateClient.write_paused`.  The orchestrator reads
this when it receives a task resume request and passes ``agent_state``
back in ``task["resume"]["agent_state"]`` so the worker can restore
``AgentState.model_validate(...)`` in a fresh sandbox.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


class _BrokerTransport:
    """HTTP-backed drop-in for the subset of the redis-py client we use.

    In *broker egress mode* the sandbox has no direct network route to
    foundation Redis; instead it reaches the narrow ``/state/v1`` façade on the
    tool-broker.  This transport implements the exact methods that
    :class:`RunStateClient` calls on ``self._client`` — ``get``, ``set(ex=)``,
    ``xadd(maxlen=, approximate=)`` and ``expire`` — translating each into a
    broker HTTP call, so the rest of the class is oblivious to the transport.

    Failure-open: every method swallows HTTP errors after a WARNING so the run
    never crashes because state could not be persisted (same contract as the
    direct Redis path).

    Keys are reconstructed from the run_id rather than parsed from the full
    Redis key string the caller passes, so the broker can re-enforce its
    per-run lease boundary.  Both the local key and the lease-bound run_id are
    derived from the same ``run_id``, so they always agree.
    """

    # Maps the canonical Redis key suffix to the façade ``kind`` value.
    _SET_KINDS = ("state", "paused", "canvas")

    def __init__(
        self,
        *,
        run_id: str,
        broker_url: str,
        tenant_id: str,
        user_id: str,
        token: str,
        lease: str,
        timeout: float = 10.0,
    ) -> None:
        self._run_id = run_id
        self._base = broker_url.rstrip("/")
        self._timeout = timeout
        self._headers = {
            "Authorization": f"Bearer {token}",
            "X-Tenant-Id": tenant_id,
            "X-User-Id": user_id,
            "X-Tool-Broker-Lease": lease,
        }

    def _kind_for_key(self, key: str) -> str | None:
        for kind in self._SET_KINDS:
            if key.endswith(f":{kind}"):
                return kind
        return None

    def get(self, key: str) -> str | None:
        kind = self._kind_for_key(key)
        if kind is None:
            return None
        try:
            import httpx

            resp = httpx.get(
                f"{self._base}/state/v1/get",
                params={"run_id": self._run_id, "kind": kind},
                headers=self._headers,
                timeout=self._timeout,
            )
            resp.raise_for_status()
            return resp.json().get("value")
        except Exception as exc:  # noqa: BLE001
            logger.warning("broker state get failed for run %s: %s", self._run_id, exc)
            return None

    def set(self, key: str, value: str, ex: int | None = None) -> None:
        kind = self._kind_for_key(key)
        if kind is None:
            return
        try:
            import httpx

            resp = httpx.post(
                f"{self._base}/state/v1/set",
                json={
                    "run_id": self._run_id,
                    "kind": kind,
                    "value": value,
                    "ttl": int(ex or 0),
                },
                headers=self._headers,
                timeout=self._timeout,
            )
            resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            logger.warning("broker state set failed for run %s: %s", self._run_id, exc)

    def xadd(
        self,
        key: str,
        fields: dict[str, str],
        *,
        maxlen: int | None = None,
        approximate: bool = True,
    ) -> None:
        del key, approximate  # key is reconstructed broker-side from run_id
        try:
            import httpx

            resp = httpx.post(
                f"{self._base}/state/v1/xadd",
                json={
                    "run_id": self._run_id,
                    "fields": {str(k): str(v) for k, v in fields.items()},
                    "maxlen": int(maxlen or 10_000),
                    # The worker calls expire(STATE_TTL_SECONDS) immediately
                    # after the first xadd; we fold the TTL into every xadd so
                    # the stream key always carries a TTL in a single
                    # round-trip (the broker EXPIRE is idempotent and cheap).
                    "expire": STATE_TTL_SECONDS,
                },
                headers=self._headers,
                timeout=self._timeout,
            )
            resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            logger.warning("broker state xadd failed for run %s: %s", self._run_id, exc)

    def expire(self, key: str, ttl: int) -> None:
        # No-op: the TTL is folded into every ``xadd`` above so the stream key
        # always has a TTL set without a second broker round-trip.
        del key, ttl

# How long (seconds) to keep a state key in Redis after terminal state.
# 30 minutes is long enough for the scheduler to poll final status; Redis
# ``allkeys-lru`` evicts this automatically if memory gets tight.
STATE_TTL_SECONDS = 1800

# Paused runs must survive long enough for a human to confirm/reject (up to
# 7 days).  The orchestrator is responsible for cleaning up the paused key
# once the resume container completes.
PAUSED_TTL_SECONDS = 7 * 24 * 3600

# Key prefix — must stay stable across deploys (scheduler reads same key).
_KEY_PREFIX = "agent:run"


def _make_key(run_id: str) -> str:
    return f"{_KEY_PREFIX}:{run_id}:state"


def _make_token_stream_key(run_id: str) -> str:
    return f"{_KEY_PREFIX}:{run_id}:tokens"


def _make_paused_key(run_id: str) -> str:
    return f"{_KEY_PREFIX}:{run_id}:paused"


def _make_canvas_key(run_id: str) -> str:
    return f"{_KEY_PREFIX}:{run_id}:canvas"


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
        self._paused_key = _make_paused_key(run_id)
        self._canvas_key = _make_canvas_key(run_id)
        self._client: Any | None = None
        self._step = 0
        # Tracks whether we have already set EXPIRE on the token stream key.
        self._token_stream_expire_set: bool = False

        if not run_id:
            logger.warning("RUN_ID unset; hot-state disabled (cannot build Redis key)")
            return

        if not redis_url:
            # Broker egress mode: no direct Redis route, but the tool-broker
            # /state/v1 façade is reachable.  Build an HTTP-backed transport
            # that exposes the same get/set/xadd/expire surface this class
            # calls on ``self._client``.  Outside broker mode (no TOOL_BROKER_URL)
            # the client stays a silent no-op exactly as before.
            self._client = _maybe_broker_transport(run_id)
            if self._client is None:
                logger.debug("REDIS_URL unset; hot-state disabled for run %s", run_id)
            else:
                logger.info(
                    "Hot-state via tool-broker for run %s (broker egress mode)",
                    run_id,
                )
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

    def push_pending_confirmation(
        self,
        tool_call_id: str,
        name: str,
        args: str,
    ) -> None:
        """Append a ``type=pending_confirmation`` entry to the token stream.

        Emitted once per pending MCP tool call when the run pauses for HITL
        confirmation.  The orchestrator SSE bridge surfaces this to the
        frontend so the user can approve or reject the tool call.

        Contract::

            type=pending_confirmation
            tool_call_id=<id>
            name=<tool name, e.g. "mcp__github__search_code">
            args=<complete JSON args string>

        Failure-open: any Redis error is logged as WARNING and swallowed.
        """
        if self._client is None:
            return
        try:
            self._client.xadd(
                self._token_stream_key,
                {
                    "type": "pending_confirmation",
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
                "Redis pending_confirmation push failed for run %s: %s",
                self._run_id,
                exc,
            )

    def push_pending_question(
        self,
        tool_call_id: str,
        question: str,
    ) -> None:
        """Append a ``type=pending_question`` entry to the token stream.

        Emitted when the agent calls ``ask_user`` (external tool) and
        the run pauses awaiting a human answer.

        Contract::

            type=pending_question
            tool_call_id=<id>
            question=<question text>

        Failure-open: any Redis error is logged as WARNING and swallowed.
        """
        if self._client is None:
            return
        try:
            self._client.xadd(
                self._token_stream_key,
                {
                    "type": "pending_question",
                    "tool_call_id": tool_call_id,
                    "question": question,
                },
                maxlen=_TOKEN_STREAM_MAXLEN,
                approximate=True,
            )
            if not self._token_stream_expire_set:
                self._client.expire(self._token_stream_key, STATE_TTL_SECONDS)
                self._token_stream_expire_set = True
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Redis pending_question push failed for run %s: %s",
                self._run_id,
                exc,
            )

    def write_paused(self, blob: dict) -> None:
        """Write the HITL pause blob to ``agent:run:{run_id}:paused``.

        ``blob`` must be a JSON-serialisable dict with at minimum:

          {
            "kind":        "confirm" | "question",
            "reply_id":    str,
            "tool_calls":  [{"id": str, "name": str, "args": str}, ...],
            "question":    str | null,
            "agent_state": <AgentState.model_dump(mode="json")>,
            "paused_at":   ISO-8601 str
          }

        TTL is ``PAUSED_TTL_SECONDS`` (7 days) — long enough for a human
        to respond.  Also updates the hot-state key status to "PAUSED".

        Failure-open: errors are logged as WARNING and swallowed.
        """
        if self._client is None:
            return
        try:
            self._client.set(
                self._paused_key,
                json.dumps(blob, ensure_ascii=False),
                ex=PAUSED_TTL_SECONDS,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Redis paused-blob write failed for run %s: %s",
                self._run_id,
                exc,
            )

    def mark_paused(self) -> None:
        """Set status=PAUSED in the hot-state key.

        Does NOT set a short TTL — paused runs must survive the 7-day
        confirmation window.  Uses ``PAUSED_TTL_SECONDS`` instead of
        ``STATE_TTL_SECONDS``.

        Failure-open: errors are logged as WARNING and swallowed.
        """
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
            state["status"] = "PAUSED"
            self._client.set(
                self._key, json.dumps(state), ex=PAUSED_TTL_SECONDS
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Redis paused-state write failed for run %s: %s",
                self._run_id,
                exc,
            )

    def push_canvas(self, payload: dict) -> int:
        """Append a canvas payload to ``agent:run:{run_id}:canvas`` (Phase B).

        The key holds a JSON *list* so multiple ``create_canvas`` tool calls in
        one run accumulate.  Each stored entry is the given ``payload`` plus a
        1-based ``seq`` field the orchestrator uses to dedupe materialization
        (idempotency: a payload with a given ``seq`` is materialized at most
        once).  The orchestrator reads this list on the streaming and reconcile
        paths and materializes any not-yet-seen entry into a canvas document.

        ``payload`` must be a JSON-serialisable dict; the canonical shape is::

            {"kind": str, "title": str, "content_markdown": str}

        Returns the ``seq`` assigned to this entry (1-based), or ``0`` when
        hot-state is disabled (no Redis / broker) so the tool can still return a
        confirmation to the model.  Failure-open: any Redis error is logged as
        WARNING and swallowed (returns 0) — a canvas write must never crash the
        run; the reply text is still delivered via the normal message path.
        """
        if self._client is None:
            return 0
        try:
            raw = self._client.get(self._canvas_key)
            items = json.loads(raw) if raw else []
            if not isinstance(items, list):
                items = []
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Redis canvas read failed for run %s: %s", self._run_id, exc
            )
            items = []

        seq = len(items) + 1
        entry = dict(payload)
        entry["seq"] = seq
        items.append(entry)
        try:
            self._client.set(
                self._canvas_key,
                json.dumps(items, ensure_ascii=False),
                ex=STATE_TTL_SECONDS,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Redis canvas write failed for run %s: %s", self._run_id, exc
            )
            return 0
        return seq

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


def _maybe_broker_transport(run_id: str) -> _BrokerTransport | None:
    """Build a broker-backed transport when in broker egress mode, else None.

    Broker mode is detected by ``TOOL_BROKER_URL`` being set while the direct
    ``REDIS_URL`` is blank (the scheduler blanks ``REDIS_URL`` and injects
    ``TOOL_BROKER_URL`` + ``TOOL_BROKER_LEASE`` for broker-mode sandboxes).
    The lease, tenant, user and bearer token are read from the same env vars
    the worker already receives.  Returns None (no-op state) if the lease is
    missing — the run continues without hot-state rather than crashing.
    """
    broker_url = os.environ.get("TOOL_BROKER_URL", "").strip()
    lease = os.environ.get("TOOL_BROKER_LEASE", "").strip()
    if not broker_url or not lease:
        return None
    return _BrokerTransport(
        run_id=run_id,
        broker_url=broker_url,
        tenant_id=os.environ.get("FOUNDATION_TENANT_ID", ""),
        user_id=os.environ.get("FOUNDATION_USER_ID", ""),
        token=os.environ.get("FOUNDATION_API_TOKEN", ""),
        lease=lease,
    )


def from_env() -> RunStateClient:
    """Build a :class:`RunStateClient` from the current process environment.

    Reads ``RUN_ID`` and ``REDIS_URL`` env vars.  Returns a no-op client
    when either is unset — callers need not check.  In broker egress mode
    (``REDIS_URL`` blank, ``TOOL_BROKER_URL`` set) the client routes hot-state
    operations through the tool-broker ``/state/v1`` façade instead.
    """
    run_id = os.environ.get("RUN_ID", "")
    redis_url = os.environ.get("REDIS_URL", "")
    return RunStateClient(run_id=run_id, redis_url=redis_url or None)
