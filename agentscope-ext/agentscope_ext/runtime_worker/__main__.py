"""``python -m agentscope_ext.runtime_worker`` entrypoint (AgentScope 2.0).

Wire contract between agent-scheduler and the sandbox
-----------------------------------------------------

* **Env vars** (set by ``services/agent-scheduler/.../docker_runtime.py``
  and ``cube_runtime.py``):

  | var | meaning |
  |---|---|
  | ``FOUNDATION_API_URL``       | base URL of foundation services |
  | ``FOUNDATION_API_TOKEN``     | short-lived per-run JWT (<= sandbox_timeout) |
  | ``FOUNDATION_TENANT_ID``     | for audit + LLM-gateway routing |
  | ``FOUNDATION_USER_ID``       | for audit attribution |
  | ``LLM_MODEL``                | which model AgentScope's Agent uses |
  | ``LLM_PROVIDER``             | ``anthropic`` (default), ``openai``, or ``deepseek`` |
  | ``LLM_API_KEY``              | provider credential (the gateway enforcer
                                  | extension supersedes this) |
  | ``OTEL_EXPORTER_OTLP_ENDPOINT`` | traces target |

* **Task delivery** (one of, in priority order):

  1. Env var ``AGENT_TASK_JSON`` -- used by the CubeSandbox runtime, which
     can't bind-mount files.  Same JSON shape as the file.
  2. File ``/workspace/task.json`` -- used by the docker runtime when the
     workspace dir is mounted.

  Shape (both): ``{"task": str, "mcp_servers": [...], "skills": [...]}``.

* **Result + events** (always written under ``/workspace``):

  | path | direction | shape |
  |---|---|---|
  | ``/workspace/result.json`` | out | ``{"status": "OK"|"FAILED", "summary": str, "error": str|null, "usage": {"input_tokens": int, "output_tokens": int, "total_tokens": int}}`` |
  | ``/workspace/events.jsonl`` | out | one JSON event per line (reply_stream events) |

* **Exit code** (read by ``DockerRuntime.get_run_status`` /
  ``CubeSandboxRuntime.get_run_status``):

  | code | meaning |
  |---|---|
  | 0   | task ran to completion (status in result.json) |
  | 1   | unrecoverable infra error (token expired, model unreachable,
  |     | AgentScope import failed) |
  | 2   | invalid task.json |
  | 137 | OOM-killed by sandbox |

B-M2 implementation
-------------------
The body builds an ``agentscope.agent.Agent`` from the hooks in
:mod:`agentscope_ext.runtime_worker.hooks` -- model, formatter, toolkit,
event sink -- then dispatches the task via ``Agent.reply_stream`` and
writes the final reply text into ``result.json``.  Each streamed event
is also appended to ``events.jsonl`` for the docker/cube runtimes to
ship back.

The sibling extensions (``event_audit_sink``, ``llm_gateway_enforcer``,
``mcp_tenant_injector``, ``skills_loader``) layer behaviour by patching
``hooks.build_model`` / ``hooks.build_formatter`` / ``hooks.build_toolkit``
/ ``hooks.attach_hooks`` -- the entrypoint itself stays small.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import os
import sys
from pathlib import Path

from agentscope_ext.runtime_worker import hooks as _hooks
from agentscope_ext.runtime_worker import redis_state as _redis

WORKSPACE = Path("/workspace")

# Secrets the scheduler delivers via /workspace/.secrets.json instead of the
# container's ``environment=`` block.  Keeping them out of the container env
# means they never appear in the kernel environ (/proc/1/environ), closing a
# leak where an in-sandbox agent reads /proc/1/environ to recover credentials
# that ``os.environ.pop()`` could not remove from the kernel-level environ.
# ONLY these keys are honoured from the file -- arbitrary keys are ignored.
_SECRET_FILE_KEYS: tuple[str, ...] = (
    "FOUNDATION_API_TOKEN",
    "TOOL_BROKER_LEASE",
    "LLM_VIRTUAL_KEY",
    "LLM_API_KEY",
    "S3_ACCESS_KEY_ID",
    "S3_SECRET_ACCESS_KEY",
    "REDIS_URL",
)

_TOOL_ENV_SCRUB_KEYS: tuple[str, ...] = (
    # Runtime/control-plane credentials and routing.
    "FOUNDATION_API_TOKEN",
    "FOUNDATION_SERVICE_SCOPES",
    "LLM_API_KEY",
    "LLM_BASE_URL",
    "LLM_GATEWAY_URL",
    "LLM_VIRTUAL_KEY",
    # Tenant/run metadata. Treat these as control-plane context, not model data.
    "FOUNDATION_API_URL",
    "FOUNDATION_TENANT_ID",
    "FOUNDATION_USER_ID",
    "RUN_ID",
    "WORKSPACE_BASE",
    # Internal service topology.
    "MCP_REGISTRY_URL",
    "SKILL_STORE_URL",
    "WEB_FETCH_GATEWAY_URL",
    "TOOL_BROKER_URL",
    "TOOL_BROKER_LEASE",
    "SANDBOX_EGRESS_MODE",
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "RUNTIME_EXTENSIONS",
    # Storage and hot-state endpoints/credentials.
    "S3_ENDPOINT_URL",
    "S3_BUCKET",
    "S3_REGION",
    "S3_ACCESS_KEY_ID",
    "S3_SECRET_ACCESS_KEY",
    "REDIS_URL",
    # Cube runtime delivers task.json here; after _read_task() it should not be
    # inherited by tool subprocesses.
    "AGENT_TASK_JSON",
)


def _scrub_process_env_for_tools() -> list[str]:
    """Remove control-plane env vars before agent tools can spawn subprocesses.

    The worker snapshots the required env values into a local dict before this
    runs. Runtime code continues to use that dict, while Bash and any stdio MCP
    subprocesses inherit a reduced environment.
    """
    removed: list[str] = []
    for key in _TOOL_ENV_SCRUB_KEYS:
        if key in os.environ:
            os.environ.pop(key, None)
            removed.append(key)
    if removed:
        logging.getLogger(__name__).info(
            "scrubbed %d control-plane env vars before tool execution",
            len(removed),
        )
    return removed


def _load_secret_file_into_env() -> int:
    """Load scheduler-delivered secrets from ``/workspace/.secrets.json`` then unlink it.

    The scheduler writes the 7 control-plane secrets (see ``_SECRET_FILE_KEYS``)
    to a mode-0600 host file that surfaces in the container as
    ``/workspace/.secrets.json`` -- a JSON object ``{"KEY": "value", ...}`` --
    instead of passing them through the container's ``environment=`` block.
    Because the secrets enter ``os.environ`` only inside this running Python
    process (userspace), the kernel environ ``/proc/1/environ`` never holds
    them, closing the /proc-read leak.

    Behaviour:

    * **Absent file** -> return quietly.  The cube runtime and the unit tests
      may not provide it; ``main()``'s ``_require_env`` gate still fails loudly
      on genuinely-missing creds such as ``FOUNDATION_API_TOKEN``.
    * **Present file** -> parse JSON (expected ``dict[str, str]``).  For each
      key in the allowlist ``_SECRET_FILE_KEYS`` set ``os.environ[key]``.  Any
      key not in the allowlist is ignored -- we never blindly trust arbitrary
      keys from the file.
    * **Always unlink** the file afterwards (even on parse failure or a
      malformed key) so the secret bytes cannot survive on the writable
      ``/workspace`` mount where the agent's Read/Bash tools could later cat it.

    Returns the number of allowlisted keys loaded (count only -- values are
    never logged).
    """
    logger = logging.getLogger(__name__)
    path = WORKSPACE / ".secrets.json"
    if not path.exists():
        return 0

    loaded = 0
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
        if not isinstance(data, dict):
            logger.warning(
                "secrets file is not a JSON object; ignoring (env gate will fail "
                "naturally on missing creds)",
            )
        else:
            for key in _SECRET_FILE_KEYS:
                if key in data:
                    os.environ[key] = str(data[key])
                    loaded += 1
    except (OSError, ValueError) as exc:
        # Parse/read failure: log a warning (no values) and still unlink below;
        # main()'s env gate then fails naturally on missing FOUNDATION_API_TOKEN.
        logger.warning("failed to load secrets file: %s: %s", type(exc).__name__, exc)
    finally:
        # Unlink unconditionally -- the secret bytes must not remain on the
        # writable workspace mount.
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:  # pragma: no cover - defensive
            logger.warning("could not unlink secrets file: %s", exc)

    if loaded:
        logger.info("loaded %d secret(s) from secrets file into env", loaded)
    return loaded


def _exit(
    code: int,
    *,
    status: str,
    summary: str,
    error: str | None = None,
    usage: dict | None = None,
    plan_steps: list | None = None,
) -> int:
    """Write result.json + return the exit code; never raises."""
    try:
        (WORKSPACE / "result.json").write_text(
            json.dumps(
                {
                    "status": status,
                    "summary": summary,
                    "error": error,
                    "usage": usage or {},
                    "plan_steps": plan_steps or [],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    except OSError:
        # If even writing the result fails the scheduler will read it as
        # FAILED via the missing-file branch; nothing more we can do.
        pass
    return code


def _read_task() -> dict:
    """Resolve the task payload via env (cube) or file (docker)."""
    inline = os.environ.get("AGENT_TASK_JSON", "").strip()
    if inline:
        return json.loads(inline)
    task_path = WORKSPACE / "task.json"
    if not task_path.is_file():
        raise ValueError(
            "task missing: AGENT_TASK_JSON env unset and /workspace/task.json absent",
        )
    return json.loads(task_path.read_text(encoding="utf-8"))


def _require_env(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise ValueError(f"required env var {name} is unset")
    return value


def _extract_reply_text(reply: object) -> str:
    """Best-effort extraction of human-readable text from an agent's final ``Msg``."""
    if reply is None:
        return ""
    getter = getattr(reply, "get_text_content", None)
    if callable(getter):
        try:
            text = getter()
            if text:
                return str(text)
        except Exception:  # pragma: no cover - defensive
            pass
    content = getattr(reply, "content", reply)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
            else:
                # Best-effort: pydantic content blocks have .text or .model_dump().
                text_attr = getattr(block, "text", None)
                if isinstance(text_attr, str) and text_attr:
                    parts.append(text_attr)
                else:
                    parts.append(str(block))
        return "\n".join(p for p in parts if p)
    return str(content)


def _extract_plan_steps(agent: object) -> list:
    """Extract structured plan steps from agent.state.tasks_context.tasks.

    Returns a list of dicts with shape ``{n, t, d, live}`` compatible with
    the frontend ``PlanStep`` type:
    * ``n``: 1-based step index
    * ``t``: task subject string
    * ``d``: True if state == "completed"
    * ``live``: True if state == "in_progress"

    Returns ``[]`` if the agent has no tasks or the attribute path is absent
    (failure-open: never raises).
    """
    try:
        tasks = agent.state.tasks_context.tasks  # type: ignore[union-attr]
        steps = []
        for i, task in enumerate(tasks, start=1):
            state = getattr(task, "state", "pending")
            steps.append({
                "n": i,
                "t": str(getattr(task, "subject", "")),
                "d": state == "completed",
                "live": state == "in_progress",
            })
        return steps
    except Exception:  # noqa: BLE001 — never crash the event loop
        return []


def _write_result_json(
    plan_steps: list,
    *,
    status: str = "RUNNING",
    summary: str = "",
    error: str | None = None,
    usage: dict | None = None,
) -> None:
    """Overwrite result.json with current plan_steps (and optional status).

    Called on every ToolCallEndEvent (throttled: only when tool events
    arrive) so the scheduler can read plan_steps while the run is still
    RUNNING.  Failure-open: any OSError is silently ignored.
    """
    try:
        (WORKSPACE / "result.json").write_text(
            json.dumps(
                {
                    "status": status,
                    "summary": summary,
                    "error": error,
                    "usage": usage or {},
                    "plan_steps": plan_steps,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    except OSError:
        pass




class _PauseSignal(Exception):
    """Internal sentinel: raised from _dispatch to signal a HITL pause.

    Attributes
    ----------
    event_type : str
        "RequireUserConfirmEvent" or "RequireExternalExecutionEvent"
    event : object
        The actual event object emitted by reply_stream.
    usage : dict
        Token usage accumulated up to the pause point.
    plan_steps : list
        Plan steps accumulated up to the pause point.
    """

    def __init__(
        self,
        event_type: str,
        event: object,
        usage: dict,
        plan_steps: list,
    ) -> None:
        super().__init__(event_type)
        self.event_type = event_type
        self.event = event
        self.usage = usage
        self.plan_steps = plan_steps


async def _dispatch(
    agent: object,
    task_input: "str | object",
    events_path: Path,
    state_client: "_redis.RunStateClient | None" = None,
) -> tuple[str, dict, list]:
    """Call the agent with the task input and return ``(reply_text, usage, plan_steps)``.

    We use ``reply_stream`` so every event lands in ``events.jsonl``;
    AgentScope 2.0's ``ReplyEndEvent`` is just a marker (no Msg attached),
    so we reconstruct the assistant text by accumulating
    ``TextBlockDeltaEvent.delta`` chunks per block id.

    ``task_input`` is either a plain task-text string (initial run) or an
    already-built event object for resume legs (``UserConfirmResultEvent``
    or ``ExternalExecutionResultEvent``).

    ``state_client`` is optional: when provided, hot-state is written to
    Redis on every event so a restart can detect the last checkpoint.

    Token counts are accumulated across all ``ModelCallEndEvent`` events
    emitted during the run (a single task may trigger multiple model calls).

    After each ``ToolCallEndEvent`` (which fires when TaskCreate/TaskUpdate
    completes) we re-read ``agent.state.tasks_context.tasks`` and overwrite
    ``result.json`` so the scheduler's RUNNING-state poll can surface
    ``plan_steps`` to the orchestrator's SSE stream.

    HITL pause detection
    --------------------
    When ``RequireUserConfirmEvent`` or ``RequireExternalExecutionEvent``
    appears in the event stream the loop raises ``_PauseSignal`` immediately
    (preferred over post-loop context inspection -- it avoids consuming any
    further events and keeps the agent state clean).  The caller
    (``_run_real``) handles the signal via ``_handle_pause``.
    """
    from agentscope.message import Msg, TextBlock

    # On a fresh run task_input is a plain string; on resume it is already
    # a resume event object (UserConfirmResultEvent / ExternalExecutionResultEvent).
    if isinstance(task_input, str):
        agent_input = Msg(
            name="user",
            content=[TextBlock(type="text", text=task_input)],
            role="user",
        )
    else:
        # Resume path: pass the event directly.
        agent_input = task_input

    # Accumulate delta chunks per text-block id; preserve insertion order
    # so concurrent blocks come out in stream order.
    text_blocks: dict[str, str] = {}
    thinking_blocks: dict[str, str] = {}
    tool_names: dict[str, str] = {}
    tool_call_args: dict[str, str] = {}
    tool_result_text: dict[str, str] = {}
    # Track last plan text (from ThinkEvent) so we can surface it in state.
    last_plan: str = ""
    # Cumulative token counts across all model calls in this run.
    input_tokens: int = 0
    output_tokens: int = 0
    # Current plan_steps — updated on every ToolCallEndEvent.
    plan_steps: list = []

    async for event in agent.reply_stream(agent_input):
        _hooks.write_event(events_path, event)
        event_type = type(event).__name__

        # HITL pause detection -- RequireUserConfirmEvent signals that one or
        # more MCP tool calls need user approval; RequireExternalExecutionEvent
        # signals that the external ask_user tool is awaiting a human answer.
        # We raise _PauseSignal immediately so the generator is abandoned
        # cleanly and the agent state captures exactly the pending tool calls.
        # Do NOT push an "end" entry to the token stream here -- the reply is
        # not finished; _handle_pause pushes pending_confirmation/question
        # entries instead.
        if event_type in (
            "RequireUserConfirmEvent",
            "RequireExternalExecutionEvent",
        ):
            usage = {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens,
            }
            plan_steps = _extract_plan_steps(agent)
            raise _PauseSignal(event_type, event, usage, plan_steps)

        # Extract plan text from ThinkEvent (AgentScope 2.x reasoning event).
        if event_type == "ThinkEvent":
            thought = getattr(event, "thought", None) or getattr(event, "text", None) or ""
            if thought:
                last_plan = str(thought)
                if state_client is not None:
                    state_client.push_thinking(last_plan)

        if event_type == "TextBlockStartEvent":
            block_id = getattr(event, "block_id", None)
            if block_id and block_id not in text_blocks:
                text_blocks[block_id] = ""
        elif event_type == "TextBlockDeltaEvent":
            block_id = getattr(event, "block_id", None) or ""
            delta = getattr(event, "delta", "") or ""
            text_blocks[block_id] = text_blocks.get(block_id, "") + str(delta)
            if delta and state_client is not None:
                state_client.push_token(str(delta))
        elif event_type == "ThinkingBlockStartEvent":
            block_id = getattr(event, "block_id", None)
            if block_id and block_id not in thinking_blocks:
                thinking_blocks[block_id] = ""
        elif event_type == "ThinkingBlockDeltaEvent":
            block_id = getattr(event, "block_id", None) or ""
            delta = getattr(event, "delta", "") or ""
            thinking_blocks[block_id] = thinking_blocks.get(block_id, "") + str(delta)
        elif event_type == "ThinkingBlockEndEvent":
            block_id = getattr(event, "block_id", None) or ""
            text = thinking_blocks.pop(block_id, "")
            if text and state_client is not None:
                state_client.push_thinking(text)
        elif event_type == "ModelCallEndEvent":
            # Accumulate token usage across multiple model calls in one run.
            input_tokens += getattr(event, "input_tokens", 0) or 0
            output_tokens += getattr(event, "output_tokens", 0) or 0
        elif event_type == "ToolCallStartEvent":
            tool_call_id = getattr(event, "tool_call_id", "") or ""
            if tool_call_id:
                tool_names[tool_call_id] = str(
                    getattr(event, "tool_call_name", None) or "tool"
                )
                tool_call_args[tool_call_id] = ""
        elif event_type == "ToolCallDeltaEvent":
            tool_call_id = getattr(event, "tool_call_id", "") or ""
            if tool_call_id:
                tool_call_args[tool_call_id] = (
                    tool_call_args.get(tool_call_id, "")
                    + str(getattr(event, "delta", "") or "")
                )
        elif event_type == "ToolCallEndEvent":
            tool_call_id = getattr(event, "tool_call_id", "") or ""
            if tool_call_id and state_client is not None:
                state_client.push_tool_call(
                    tool_call_id,
                    tool_names.get(tool_call_id, "tool"),
                    tool_call_args.get(tool_call_id, ""),
                )
            # After each tool call, re-read the task list so plan changes
            # (TaskCreate, TaskUpdate) are immediately visible.  Write the
            # updated plan to result.json so the scheduler's RUNNING-state
            # poll can relay it upstream via SSE.
            plan_steps = _extract_plan_steps(agent)
            if plan_steps:
                _write_result_json(
                    plan_steps,
                    status="RUNNING",
                    usage={"input_tokens": input_tokens, "output_tokens": output_tokens, "total_tokens": input_tokens + output_tokens},
                )
        elif event_type == "ToolResultStartEvent":
            tool_call_id = getattr(event, "tool_call_id", "") or ""
            if tool_call_id:
                tool_names[tool_call_id] = str(
                    getattr(event, "tool_call_name", None)
                    or tool_names.get(tool_call_id)
                    or "tool"
                )
                tool_result_text[tool_call_id] = ""
        elif event_type == "ToolResultTextDeltaEvent":
            tool_call_id = getattr(event, "tool_call_id", "") or ""
            if tool_call_id:
                tool_result_text[tool_call_id] = (
                    tool_result_text.get(tool_call_id, "")
                    + str(getattr(event, "delta", "") or "")
                )
        elif event_type == "ToolResultDataDeltaEvent":
            tool_call_id = getattr(event, "tool_call_id", "") or ""
            if tool_call_id:
                media_type = getattr(event, "media_type", "") or "application/octet-stream"
                tool_result_text[tool_call_id] = (
                    tool_result_text.get(tool_call_id, "")
                    + f"[data: {media_type}]"
                )
        elif event_type == "ToolResultEndEvent":
            tool_call_id = getattr(event, "tool_call_id", "") or ""
            if tool_call_id and state_client is not None:
                state_client.push_tool_result(
                    tool_call_id,
                    tool_names.get(tool_call_id, "tool"),
                    tool_result_text.get(tool_call_id, ""),
                    str(getattr(event, "state", "") or ""),
                )

        # Write hot state after every event (failure-open).
        if state_client is not None:
            state_client.write_step(plan=last_plan, cursor=event_type)

    # Final extraction in case no ToolCallEndEvent fired (simple tasks).
    plan_steps = _extract_plan_steps(agent)

    reply_text = "\n".join(t for t in text_blocks.values() if t).strip()
    usage = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
    }
    if state_client is not None:
        state_client.push_token_end()
    return reply_text, usage, plan_steps


class _BrokerS3Body:
    """``Body``-like wrapper so broker reads look identical to boto3's."""

    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data


class _BrokerS3Client:
    """Read-only S3 shim that routes ``get_object`` through the tool-broker.

    Exposes the single boto3 call site the worker uses — ``get_object`` —
    returning ``{"Body": <obj with .read()>}`` so ``_s3_read_text`` and
    ``_download_attachments`` need no changes.  Other boto3 operations (e.g.
    ``put_object``) are intentionally absent: the broker S3 façade is
    read-only, and artifact upload happens scheduler-side, not in-sandbox.

    Failure-open: a missing object (broker 404) or any HTTP error raises an
    exception, exactly like boto3's ``NoSuchKey`` — the callers already catch
    broad exceptions and treat them as "object absent".
    """

    def __init__(
        self,
        *,
        broker_url: str,
        tenant_id: str,
        user_id: str,
        token: str,
        lease: str,
        timeout: float = 30.0,
    ) -> None:
        self._base = broker_url.rstrip("/")
        self._timeout = timeout
        self._headers = {
            "Authorization": f"Bearer {token}",
            "X-Tenant-Id": tenant_id,
            "X-User-Id": user_id,
            "X-Tool-Broker-Lease": lease,
        }

    def get_object(self, Bucket: str, Key: str) -> dict:  # noqa: N803
        import httpx

        resp = httpx.get(
            f"{self._base}/s3/v1/object",
            params={"bucket": Bucket, "key": Key},
            headers=self._headers,
            timeout=self._timeout,
        )
        resp.raise_for_status()
        return {"Body": _BrokerS3Body(resp.content)}


def _s3_client(env: dict):
    """Build an S3 client (boto3 or broker shim) from the env.

    Returns ``None`` (failure-open) if boto3 is unavailable or credentials
    are missing.  Callers must guard with ``if client is None: return ...``.

    In *broker egress mode* (``S3_ENDPOINT_URL`` blank, ``TOOL_BROKER_URL``
    set) object reads are routed through the tool-broker ``/s3/v1`` façade via
    :class:`_BrokerS3Client`, so the sandbox never needs a direct object-store
    route or credentials.

    Otherwise we build a boto3 client, always passing the S3_* credentials
    explicitly because boto3 defaults to looking for
    AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY; those names are NOT injected
    into the sandbox, so relying on boto3's auto-discovery would silently
    produce an unauthenticated client that fails every call.
    """
    import warnings

    endpoint_url = env.get("S3_ENDPOINT_URL") or None
    broker_url = (env.get("TOOL_BROKER_URL") or "").strip()
    lease = (env.get("TOOL_BROKER_LEASE") or "").strip()

    # Broker egress mode: no direct object-store endpoint, but the broker
    # /s3/v1 façade is reachable with the per-run lease.
    if not endpoint_url and broker_url and lease:
        return _BrokerS3Client(
            broker_url=broker_url,
            tenant_id=env.get("FOUNDATION_TENANT_ID", ""),
            user_id=env.get("FOUNDATION_USER_ID", ""),
            token=env.get("FOUNDATION_API_TOKEN", ""),
            lease=lease,
        )

    region = env.get("S3_REGION") or "us-east-1"
    access_key = env.get("S3_ACCESS_KEY_ID") or None
    secret_key = env.get("S3_SECRET_ACCESS_KEY") or None

    try:
        import boto3  # type: ignore[import-not-found]

        return boto3.client(
            "s3",
            region_name=region,
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
        )
    except Exception as exc:
        warnings.warn(f"boto3 unavailable; S3 operations disabled: {exc}", stacklevel=3)
        return None


def _s3_read_text(s3_client, bucket: str, key: str) -> str | None:
    """Read an S3 object and return its UTF-8 text content.

    Returns ``None`` if the object does not exist or any error occurs
    (failure-open contract: missing files are not fatal).
    """
    import logging

    logger = logging.getLogger(__name__)
    try:
        obj = s3_client.get_object(Bucket=bucket, Key=key)
        return obj["Body"].read().decode("utf-8")
    except Exception as exc:  # noqa: BLE001
        # NoSuchKey is the expected path for tenants without custom prompts.
        logger.debug("S3 read skipped for key %s: %s", key, exc)
        return None


def _canvas_tool_enabled(env: dict) -> bool:
    """True when the ``canvas_tool`` extension is active for this run.

    The extension (and thus the ``create_canvas`` tool) is only registered when
    ``canvas_tool`` appears in ``RUNTIME_EXTENSIONS`` — the scheduler appends it
    per-run behind the ``enable_canvas_tool`` feature flag.  We gate the
    system-prompt instruction on the same signal so the tool and its usage
    guidance always appear/disappear together (flag off ⇒ zero change).
    """
    raw = env.get("RUNTIME_EXTENSIONS", "") or ""
    return "canvas_tool" in {part.strip() for part in raw.split(",")}


def _load_prompt_files(env: dict, task: dict) -> str:
    """Load soul / identity / user prompt files from S3 and assemble a system prompt.

    Key layout (all under bucket ``S3_BUCKET``):

    * ``tenant/{tenant_id}/prompts/identity.md`` — who the agent is
    * ``tenant/{tenant_id}/prompts/soul.md``     — personality / style
    * ``tenant/{tenant_id}/users/{user_id}/user.md`` — per-user preferences

    Missing files are silently skipped.  If *none* of the files exist and the
    task has no ``sys_prompt``, a sensible Chinese default is returned.

    The final order is: identity → soul → user → task sys_prompt.
    """
    bucket = env.get("S3_BUCKET") or "agent-platform"
    tenant_id = env.get("FOUNDATION_TENANT_ID") or ""
    user_id = env.get("FOUNDATION_USER_ID") or ""

    client = _s3_client(env)

    parts: list[str] = []

    if client and tenant_id:
        identity = _s3_read_text(client, bucket, f"tenant/{tenant_id}/prompts/identity.md")
        if identity:
            parts.append(identity.strip())

        soul = _s3_read_text(client, bucket, f"tenant/{tenant_id}/prompts/soul.md")
        if soul:
            parts.append(soul.strip())

        if user_id:
            user_pref = _s3_read_text(
                client, bucket, f"tenant/{tenant_id}/users/{user_id}/user.md"
            )
            if user_pref:
                parts.append(user_pref.strip())

    # Task-level sys_prompt from the orchestrator (optional).
    task_sys = str(task.get("sys_prompt") or "").strip()
    if task_sys:
        parts.append(task_sys)

    if parts:
        return "\n\n".join(parts)

    # Absolute fallback — always produces a usable agent even with no S3 config.
    return (
        "你是一个企业级 AI 助理，运行于安全沙箱中。"
        "请使用可用工具完成用户的任务，并以简洁的中文回复你的执行摘要。"
    )


def _download_attachments(task: dict, env: dict) -> list[str]:
    """Download task.json ``files`` to /workspace/inputs/.

    Returns list of local filenames that were downloaded.
    Failure-open: individual failures are warned, not raised.
    """
    files = task.get("files") or []
    if not files:
        return []

    import logging
    import warnings

    logger = logging.getLogger(__name__)

    bucket = env.get("S3_BUCKET") or "agent-platform"

    s3 = _s3_client(env)
    if s3 is None:
        warnings.warn("boto3 unavailable; attachments will not be downloaded", stacklevel=2)
        return []

    inputs_dir = WORKSPACE / "inputs"
    try:
        inputs_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning("cannot create /workspace/inputs: %s", exc)
        return []

    downloaded: list[str] = []
    for file_spec in files:
        key = file_spec.get("key", "")
        filename = file_spec.get("filename", "") or key.split("/")[-1] or "file"
        dest = inputs_dir / filename
        try:
            obj = s3.get_object(Bucket=bucket, Key=key)
            dest.write_bytes(obj["Body"].read())
            downloaded.append(filename)
            logger.info("downloaded attachment: %s -> %s", key, dest)
        except Exception as exc:  # noqa: BLE001
            logger.warning("failed to download attachment %s: %s", key, exc)

    return downloaded




# ---------------------------------------------------------------------------
# HITL helpers -- permission context, ask_user tool, pause serialisation
# ---------------------------------------------------------------------------

# Native sandbox tool names that should be auto-allowed without confirmation.
# These are the tools registered by _workspace_tools.register_into().
_NATIVE_TOOL_NAMES = ("Read", "Grep", "Glob", "Write", "Edit", "Bash")


def _build_hitl_permission_context() -> "object":
    """Build a PermissionContext for HITL-enabled runs.

    Permission design (engine source justification)
    -----------------------------------------------
    *  PermissionMode.BYPASS skips all checks (step 5 in engine) and
       auto-approves every tool -- unsuitable for HITL.
    *  PermissionMode.DEFAULT falls through to _default_decision_ask
       (step 7) for any tool not matched by earlier rules, which returns ASK.
    *  PermissionMode.DONT_ASK converts ASK->DENY -- also unsuitable.

    So we use DEFAULT and add explicit ALLOW rules for every built-in
    native tool (by tool name, with rule_content=None = match everything).
    MCP tools are named mcp__{server}__{tool} -- they will not match any
    allow rule and will hit the default ASK path, causing reply_stream to
    emit RequireUserConfirmEvent and pause.

    Returns the constructed PermissionContext, or None on ImportError
    (old AgentScope, no permission sub-system).
    """
    try:
        from agentscope.permission import (
            PermissionContext,
            PermissionMode,
            PermissionRule,
            PermissionBehavior,
        )

        ctx = PermissionContext(mode=PermissionMode.DEFAULT)
        for tool_name in _NATIVE_TOOL_NAMES:
            rule = PermissionRule(
                tool_name=tool_name,
                rule_content=None,  # None = match every invocation of this tool
                behavior=PermissionBehavior.ALLOW,
                source="hitl_sandbox_policy",
            )
            ctx.allow_rules.setdefault(tool_name, []).append(rule)
        return ctx
    except Exception as exc:  # noqa: BLE001
        logging.getLogger(__name__).warning(
            "HITL: could not build DEFAULT permission context; "
            "falling back to BYPASS: %s",
            exc,
        )
        return None


def _build_bypass_permission_context() -> "object":
    """Build a PermissionContext with BYPASS for non-HITL runs.

    Used when task.get("hitl", True) is False (scheduled / background
    runs where no human is present to confirm tool calls).
    """
    try:
        from agentscope.permission import PermissionContext, PermissionMode

        return PermissionContext(mode=PermissionMode.BYPASS)
    except Exception as exc:  # noqa: BLE001
        logging.getLogger(__name__).warning(
            "could not set BYPASS permission; tool calls may require confirmation: %s",
            exc,
        )
        return None


def _build_ask_user_tool() -> "object | None":
    """Build and return the ask_user external tool, or None on failure.

    External tool API (verified against agentscope source)
    -------------------------------------------------------
    ToolBase.is_external_tool = True signals that the tool has no
    callable __call__ implementation; instead, when the permission
    engine processes it the agent yields RequireExternalExecutionEvent
    and pauses, awaiting an ExternalExecutionResultEvent with the
    human's answer.

    Returns the tool instance, or None if AgentScope's tool sub-system is
    unavailable (failure-open: the run proceeds without ask_user).
    """
    try:
        from agentscope.tool import ToolBase
        from agentscope.permission import PermissionContext, PermissionDecision, PermissionBehavior

        class AskUserTool(ToolBase):
            """Interactive clarification tool -- pauses the run for a human answer."""

            name: str = "ask_user"
            description: str = (
                "Ask the user a clarifying question and wait for their response. "
                "Use this when you are uncertain about a key requirement, ambiguous "
                "instruction, or need explicit confirmation before proceeding with "
                "an irreversible action. Provide a clear, concise question."
            )
            input_schema: dict = {
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "The question to ask the user.",
                    },
                },
                "required": ["question"],
            }
            is_concurrency_safe: bool = False
            is_read_only: bool = True  # asking is non-destructive
            is_external_tool: bool = True  # triggers RequireExternalExecutionEvent

            async def check_permissions(
                self,
                tool_input: dict,
                context: PermissionContext,
            ) -> PermissionDecision:
                # External tools are always allowed to pause (the pause IS
                # the execution); return ALLOW so the engine emits the
                # external execution event.
                return PermissionDecision(
                    behavior=PermissionBehavior.ALLOW,
                    message="ask_user: external execution allowed",
                )

            async def __call__(self, **kwargs):  # pragma: no cover
                # Never actually called -- the agent pauses before invocation.
                raise NotImplementedError("ask_user is an external tool; __call__ should not be invoked")

        return AskUserTool()
    except Exception as exc:  # noqa: BLE001
        logging.getLogger(__name__).warning(
            "HITL: could not build ask_user tool: %s", exc
        )
        return None


def _handle_pause(
    *,
    event_type: str,
    event: object,
    agent: object,
    run_id: str,
    state_client: "_redis.RunStateClient",
    usage: dict,
    plan_steps: list,
) -> bool:
    """Serialise a HITL pause and return True if the run should pause.

    Called from _dispatch when RequireUserConfirmEvent or
    RequireExternalExecutionEvent is detected in the event stream.

    Steps
    -----
    1. Extract pending tool_call(s) from the event.
    2. Determine kind: "confirm" for RequireUserConfirmEvent,
       "question" for RequireExternalExecutionEvent (ask_user).
    3. Push pending_confirmation / pending_question entries to the
       token stream (one per pending tool call).
    4. Serialise agent.state with model_dump(mode="json") and write
       agent:run:{run_id}:paused via state_client.write_paused.
    5. Update hot state to "PAUSED" via state_client.mark_paused.
    6. Write result.json with status="paused".

    Returns True on success, False on any internal error (caller falls back
    to treating the run as a normal failure -- failure-open contract).
    """
    logger = logging.getLogger(__name__)

    try:
        # ----------------------------------------------------------------
        # 1. Extract pending tool calls and reply_id from the event.
        # ----------------------------------------------------------------
        reply_id: str = getattr(event, "reply_id", "") or ""
        raw_tool_calls: list = list(getattr(event, "tool_calls", []) or [])
        kind = (
            "question"
            if event_type == "RequireExternalExecutionEvent"
            else "confirm"
        )

        tool_call_dicts: list[dict] = []
        for tc in raw_tool_calls:
            tc_id = str(getattr(tc, "id", "") or "")
            tc_name = str(getattr(tc, "name", "") or "")
            tc_input = getattr(tc, "input", None)
            if isinstance(tc_input, str):
                tc_args = tc_input
            else:
                try:
                    tc_args = json.dumps(tc_input or {}, ensure_ascii=False)
                except Exception:  # noqa: BLE001
                    tc_args = str(tc_input or {})
            tool_call_dicts.append({"id": tc_id, "name": tc_name, "args": tc_args})

        # ----------------------------------------------------------------
        # 2. Push pending_confirmation / pending_question to the token stream.
        #    For ask_user (kind=question), extract the question text from args.
        # ----------------------------------------------------------------
        for tc in tool_call_dicts:
            if kind == "question":
                # The question lives in the args JSON under "question" key.
                try:
                    question_text = json.loads(tc["args"]).get("question", "")
                except Exception:  # noqa: BLE001
                    question_text = tc["args"]
                state_client.push_pending_question(
                    tool_call_id=tc["id"],
                    question=question_text,
                )
            else:
                state_client.push_pending_confirmation(
                    tool_call_id=tc["id"],
                    name=tc["name"],
                    args=tc["args"],
                )

        # ----------------------------------------------------------------
        # 3. Serialise agent state.  mode="json" is MANDATORY -- plain
        #    model_dump writes enum reprs that fail model_validate on reload.
        # ----------------------------------------------------------------
        agent_state_dict: dict = {}
        try:
            agent_state_dict = agent.state.model_dump(mode="json")  # type: ignore[union-attr]
        except Exception as exc:  # noqa: BLE001
            logger.warning("HITL: could not serialise agent state: %s", exc)
            return False

        # ----------------------------------------------------------------
        # 4. Write paused blob.
        # ----------------------------------------------------------------
        question_str: str | None = None
        if kind == "question" and tool_call_dicts:
            try:
                question_str = json.loads(tool_call_dicts[0]["args"]).get("question")
            except Exception:  # noqa: BLE001
                question_str = tool_call_dicts[0]["args"]

        paused_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        blob = {
            "kind": kind,
            "reply_id": reply_id,
            "tool_calls": tool_call_dicts,
            "question": question_str,
            "agent_state": agent_state_dict,
            "paused_at": paused_at,
        }
        state_client.write_paused(blob)

        # ----------------------------------------------------------------
        # 5. Update hot state -> PAUSED.
        # ----------------------------------------------------------------
        state_client.mark_paused()

        # ----------------------------------------------------------------
        # 6. Write result.json with status="paused".
        # ----------------------------------------------------------------
        _write_result_json(
            plan_steps,
            status="paused",
            usage=usage,
        )

        logger.info(
            "HITL: run %s paused (%s) -- %d pending tool call(s)",
            run_id,
            kind,
            len(tool_call_dicts),
        )
        return True

    except Exception as exc:  # noqa: BLE001
        logger.warning("HITL: _handle_pause failed: %s", exc)
        return False

def _run_real(task: dict) -> tuple[str, str, dict, list]:
    """Build an Agent from the configured hooks and run the task.

    Returns ``(status, summary, usage, plan_steps)``.  Raises only for
    unrecoverable infra failures (caught in :func:`main` and surfaced as
    exit-1).  ``usage`` is a dict with keys ``input_tokens``,
    ``output_tokens``, ``total_tokens``.  ``plan_steps`` is a list of
    ``{n, t, d, live}`` dicts (empty list when the agent made no tasks).

    HITL flag
    ---------
    ``task.get("hitl", True)`` controls whether tool-call confirmation is
    enabled.  When True (the default), native sandbox tools (Read/Write/
    Bash/Edit/Grep/Glob) are auto-allowed; MCP-injected tools pause the run
    for human approval.  When False, BYPASS mode is used (all tools
    auto-approved -- suitable for scheduled/background runs).

    Resume path
    -----------
    ``task.get("resume")`` carries the orchestrator's resume payload::

        {
          "kind":        "confirm" | "answer",
          "tool_call_id": str,
          "confirmed":   bool | null,
          "answer":      str | null,
          "agent_state": <AgentState.model_dump(mode="json")>
        }

    On resume the worker restores ``AgentState`` from the saved dict
    (bypassing history-rebuild and checkpoint-resume logic) and drives
    ``reply_stream`` with the appropriate confirm/answer event.  The run_id
    is reused so tokens stream to the same Redis stream.  A resumed run can
    pause again (e.g. second MCP call) -- the pause path is reachable on
    every leg.
    """
    logger = logging.getLogger(__name__)

    # ------------------------------------------------------------------
    # HITL flag -- gating variable for the whole permission policy.
    # ------------------------------------------------------------------
    hitl_enabled: bool = bool(task.get("hitl", True))

    # ------------------------------------------------------------------
    # Resume detection: task["resume"] is present when the orchestrator is
    # driving a HITL resume leg.  We detect it early so we can skip the
    # checkpoint/memory injection that is only relevant for fresh runs.
    # ------------------------------------------------------------------
    resume_payload: dict | None = task.get("resume") or None

    text = str(task.get("task", "")).strip()
    # Allow empty task text on resume legs (the agent resumes from its
    # serialised state, so the original task text is only needed to build
    # the initial Msg -- on resume we pass the confirm/answer event instead).
    if not text and resume_payload is None:
        return "FAILED", "task text is empty", {}, []

    # Local imports so this module is importable without ``agentscope``;
    # the ImportError flows up to ``main`` and turns into exit code 1.
    from agentscope.agent import Agent

    env = _hooks.snapshot_env()
    tenant_id = env.get("FOUNDATION_TENANT_ID", "")
    run_id = env.get("RUN_ID", "")

    # ------------------------------------------------------------------
    # Hot-state: connect to Redis.
    # failure-open -- RunStateClient swallows all Redis errors internally.
    # ------------------------------------------------------------------
    state_client = _redis.from_env()
    # Share the pre-scrub client with late consumers (canvas tool et al.):
    # after _scrub_process_env_for_tools() removes REDIS_URL / broker lease
    # from os.environ, any later from_env() yields a silent no-op client.
    _redis.set_shared_client(state_client)
    _scrub_process_env_for_tools()

    # ------------------------------------------------------------------
    # For fresh (non-resume) runs only: detect a Redis checkpoint and
    # inject a memory note so the agent knows it is restarting from a
    # previous crash-resume (different from HITL pause-resume).
    # ------------------------------------------------------------------
    if resume_payload is None:
        checkpoint = state_client.read_checkpoint()
        if checkpoint is not None and checkpoint.get("status") not in (
            "COMPLETED", "FAILED", "PAUSED"
        ):
            prior_step = checkpoint.get("step", 0)
            prior_plan = checkpoint.get("plan", "")
            logger.info(
                "resuming from step %d (prior plan: %.120s...)",
                prior_step,
                prior_plan,
            )
            existing_memory = str(task.get("memory") or "").strip()
            resume_note = (
                "[\u7cfb\u7edf\uff1a\u672c\u6b21\u8fd0\u884c\u662f\u4ece\u65ad\u70b9\u6062\u590d\uff0c\u4e0a\u6b21\u5df2\u5b8c\u6210 "
                + str(prior_step)
                + " \u6b65\u3002"
                + ("\u4e0a\u6b21\u8ba1\u5212\uff1a" + prior_plan if prior_plan else "")
                + "]"
            )
            task = dict(task)
            task["memory"] = (resume_note + "\n\n" + existing_memory).strip() if existing_memory else resume_note

    # Download user-uploaded attachments before building the agent so the
    # agent can reference them in its context.  Failure-open: missing files
    # log a warning but don't abort the run.
    downloaded = _download_attachments(task, env)

    model = _hooks.build_model(task, env)
    toolkit = _hooks.build_toolkit(task, tenant_id, env)

    # ------------------------------------------------------------------
    # HITL: register the ask_user external tool when HITL is enabled.
    # The tool is added to the toolkit's basic ToolGroup alongside the
    # built-in workspace tools.  Failure-open: if the tool cannot be built
    # (old AgentScope, missing ToolBase) the run proceeds without it.
    # ------------------------------------------------------------------
    if hitl_enabled:
        ask_user_tool = _build_ask_user_tool()
        if ask_user_tool is not None:
            try:
                toolkit.tool_groups[0].tools.append(ask_user_tool)
                logger.debug("HITL: registered ask_user external tool")
            except Exception as exc:  # noqa: BLE001
                logger.warning("HITL: could not register ask_user tool: %s", exc)

    # Build system prompt: soul/identity/user files from S3 + task-level override.
    sys_prompt = _load_prompt_files(env, task)

    # Inject long-term memory snippets (pre-fetched by orchestrator, stored in
    # task.json["memory"]).  These arrive as a plain string already formatted
    # by the orchestrator; we prepend them so the agent sees them before the
    # main instructions.
    #
    # Scope note: the orchestrator excludes THIS session's own memory documents
    # from the search (exclude_origin_session_id), so these snippets are
    # cross-session RAG memory only.  The current session's prior turns are fed
    # back separately via the restored conversation history below — keeping them
    # out of here avoids double-injecting the same Q&A into both the system
    # prompt and the agent context.
    memory_snippets = str(task.get("memory") or "").strip()
    if memory_snippets:
        sys_prompt = f"## \u76f8\u5173\u5386\u53f2\u8bb0\u5fc6\n\n{memory_snippets}\n\n---\n\n{sys_prompt}"

    # Append uploaded-file context if any were downloaded.
    if downloaded:
        file_list = ", ".join(downloaded)
        sys_prompt += (
            f"\n\n\u7528\u6237\u4e0a\u4f20\u4e86\u4ee5\u4e0b\u6587\u4ef6\uff0c\u4f4d\u4e8e /workspace/inputs/\uff1a{file_list}\n"
            "\u8bf7\u4f7f\u7528 Read \u5de5\u5177\u8bfb\u53d6 /workspace/inputs/ \u4e0b\u7684\u6587\u4ef6\u6765\u5b8c\u6210\u4efb\u52a1\u3002"
        )

    # Pre-create /workspace/outputs/ so the agent can write there without
    # needing to mkdir first (though Bash mkdir also works).
    outputs_dir = WORKSPACE / "outputs"
    try:
        outputs_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning("cannot pre-create /workspace/outputs: %s", exc)

    sys_prompt += (
        "\n\n## \u5173\u4e8e\u6587\u4ef6\u8f93\u51fa\n"
        "\u9ed8\u8ba4\u60c5\u51b5\u4e0b\uff0c\u8bf7\u628a\u7ed3\u679c\uff08\u5305\u62ec\u4ee3\u7801\u3001\u6587\u672c\u3001\u8868\u683c\u7b49\uff09\u76f4\u63a5\u5199\u5728\u56de\u590d\u6b63\u6587\u91cc\uff1a"
        "\u4ee3\u7801\u7528 Markdown \u4ee3\u7801\u5757\uff08```\u8bed\u8a00 ... ```\uff09\u5c55\u793a\uff0c\u4fbf\u4e8e\u7528\u6237\u76f4\u63a5\u9605\u8bfb\u548c\u590d\u5236\uff1b"
        "\u4e0d\u8981\u4e3a\u6b64\u521b\u5efa\u4efb\u4f55\u6587\u4ef6\u3002\n"
        "\u53ea\u6709\u5f53\u7528\u6237\u660e\u786e\u8981\u6c42\u201c\u4ea4\u4ed8/\u751f\u6210/\u5bfc\u51fa/\u4e0b\u8f7d/\u4fdd\u5b58\u4e3a\u6587\u4ef6\u201d\uff08\u4f8b\u5982\u201c\u751f\u6210\u4e00\u4e2a .docx\u201d\u3001"
        "\u201c\u628a\u5b83\u5bfc\u51fa\u6210 Excel\u201d\u3001\u201c\u7ed9\u6211\u4e00\u4e2a txt \u6587\u4ef6\u201d\uff09\u65f6\uff0c\u624d\u4f7f\u7528 Write \u5de5\u5177\u628a\u4ea7\u7269\u5199\u5165 "
        "/workspace/outputs/ \u76ee\u5f55\uff08\u8def\u5f84\u793a\u4f8b\uff1a/workspace/outputs/report.docx\uff09\uff1b"
        "\u4efb\u52a1\u7ed3\u675f\u540e\u8be5\u76ee\u5f55\u4e0b\u7684\u6587\u4ef6\u4f1a\u81ea\u52a8\u4f5c\u4e3a\u4ea7\u7269\u56de\u4f20\u7ed9\u7528\u6237\u3002\u5982\u9700\u5148\u521b\u5efa\u5b50\u76ee\u5f55\uff0c\u53ef\u7528 Bash \u6267\u884c mkdir -p\u3002\n"
        "\u5224\u65ad\u4e0d\u786e\u5b9a\u65f6\uff0c\u4f18\u5148\u76f4\u63a5\u5728\u6b63\u6587\u56de\u590d\uff0c\u800c\u4e0d\u662f\u5199\u6587\u4ef6\u3002"
    )

    # HITL: tell the model HOW to ask the user.  Without this the model asks
    # clarification questions as plain reply text and keeps going (or stops
    # with an unanswerable question) instead of calling the ask_user tool,
    # so the pause/resume machinery never engages.
    if hitl_enabled:
        sys_prompt += (
            "\n\n## \u5411\u7528\u6237\u63d0\u95ee\uff08ask_user\uff09\n"
            "\u5f53\u4f60\u9700\u8981\u7528\u6237\u6f84\u6e05\u9700\u6c42\u3001\u5728\u591a\u4e2a\u65b9\u6848\u4e2d\u505a\u9009\u62e9\uff0c"
            "\u6216\u7f3a\u5c11\u7ee7\u7eed\u6267\u884c\u6240\u5fc5\u9700\u7684\u4fe1\u606f\u65f6\uff0c"
            "\u5fc5\u987b\u8c03\u7528 ask_user \u5de5\u5177\u63d0\u95ee\u5e76\u7b49\u5f85\u7528\u6237\u56de\u7b54\uff1b"
            "\u4e0d\u8981\u5728\u6b63\u6587\u91cc\u63d0\u95ee\u540e\u81ea\u884c\u731c\u6d4b\u7ee7\u7eed\uff0c"
            "\u4e5f\u4e0d\u8981\u628a\u95ee\u9898\u4f5c\u4e3a\u6700\u7ec8\u56de\u590d\u7ed3\u675f\u4efb\u52a1\u3002"
            "\u7528\u6237\u56de\u7b54\u540e\u4f1a\u81ea\u52a8\u7ee7\u7eed\u672c\u6b21\u4efb\u52a1\u3002"
        )

    # Canvas (canvas v2, Phase B): tell the model WHEN to promote a long-form
    # deliverable into an editable canvas via ``create_canvas``, and that after
    # doing so the reply body must be a single short pointer sentence.  Gated on
    # the ``canvas_tool`` extension being active (the tool is only registered
    # then) so the instruction and the tool appear/disappear together \u2014 flag off
    # \u21d2 this block is skipped entirely.
    if _canvas_tool_enabled(env):
        sys_prompt += (
            "\n\n## \u957f\u6587\u6210\u6587\u521b\u4f5c\uff1a\u5fc5\u987b\u7528\u753b\u5e03\uff08create_canvas\uff09\n"
            "\u8fd9\u662f\u4e00\u6761\u786c\u6027\u89c4\u5219\u3002\u51e1\u662f\u300c\u6210\u6587\u521b\u4f5c\u7c7b\u300d\u8bf7\u6c42\u2014\u2014"
            "\u5199\u6587\u7ae0\u3001\u77ed\u6587\u3001\u62a5\u544a\u3001\u7b80\u62a5\u3001\u65b9\u6848\u3001\u90ae\u4ef6\u3001\u6587\u6848\u3001\u603b\u7ed3\u3001"
            "\u6f14\u8bb2\u7a3f\u3001\u6545\u4e8b\u3001\u793e\u5a92\u957f\u6587\u3001\u4ee3\u7801\u7b14\u8bb0\u7b49\uff0c"
            "\u4ee5\u53ca\u4efb\u4f55\u300a\u4e66\u540d\u53f7\u300b\u547d\u9898\u5199\u4f5c\u2014\u2014"
            "\u552f\u4e00\u6b63\u786e\u7684\u505a\u6cd5\u662f\uff1a\u5148\u8c03\u7528 create_canvas \u5de5\u5177\uff0c"
            "\u628a\u6807\u9898\uff08\u4ece\u547d\u9898\u6216\u4e66\u540d\u53f7\u91cc\u63d0\u53d6\uff09\u548c**\u5b8c\u6574\u6b63\u6587**\u653e\u8fdb content_markdown\u3002\n"
            "\u7981\u6b62\uff1a\u4e0d\u8981\u5148\u5728\u6b63\u6587\u91cc\u628a\u6587\u7ae0\u5199\u51fa\u6765\u518d\u8c03\u7528\uff1b"
            "\u4e0d\u8981\u4e00\u8fb9\u8c03\u7528\u4e00\u8fb9\u5728\u6b63\u6587\u91cd\u590d\u5168\u6587\u3002"
            "\u8c03\u7528\u5de5\u5177\u540e\uff0c\u4f60\u7684\u6b63\u6587\u56de\u590d**\u53ea\u80fd\u5199\u4e00\u53e5\u8bdd**\u8bf4\u660e\uff0c"
            "\u4f8b\u5982\u300c\u5df2\u4e3a\u4f60\u521b\u4f5c\u300aXX\u300b\uff0c\u5185\u5bb9\u5728\u53f3\u4fa7\u753b\u5e03\u4e2d\uff0c\u53ef\u67e5\u770b\u548c\u7f16\u8f91\u300d\u3002\n"
            "\u53ea\u6709\u8fd9\u4e9b\u624d\u4e0d\u8c03\u7528\u3001\u76f4\u63a5\u5728\u6b63\u6587\u56de\u590d\uff1a"
            "\u95f2\u804a\u3001\u7b80\u77ed\u95ee\u7b54\u3001\u89e3\u91ca\u8bf4\u660e\u3001\u6e05\u5355\u3001\u8ba1\u7b97\u3001\u4ee3\u7801\u8c03\u8bd5\u3001"
            "\u4f9b\u9605\u8bfb\u7684\u77ed\u4ee3\u7801\u7247\u6bb5\u2014\u2014\u5373\u7528\u6237\u5e76\u975e\u8981\u4e00\u7bc7\u6210\u6587\u4f5c\u54c1\u65f6\u3002\n"
            "\u793a\u4f8b\uff1a\u7528\u6237\u8bf4\u300c\u5199\u4e00\u7bc7\u300aAI \u4e0e\u5c31\u4e1a\u300b\u77ed\u6587\uff0c\u7ea6300\u5b57\u300d"
            "\u2192 \u7acb\u5373\u8c03\u7528 create_canvas\uff08title=\u300cAI \u4e0e\u5c31\u4e1a\u300d\uff0c"
            "content_markdown=\u5b8c\u6574300\u5b57\u6b63\u6587\uff09\uff0c\u6b63\u6587\u53ea\u56de\u4e00\u53e5\u8bf4\u660e\u3002"
        )

    # ------------------------------------------------------------------
    # Available skills (progressive disclosure).
    # ------------------------------------------------------------------
    try:
        from agentscope_ext.skills_loader import collect_skill_prompts

        skill_prompts = collect_skill_prompts(task, tenant_id, env)
    except Exception as exc:  # noqa: BLE001 -- never let skills crash the run
        logger.warning("skills_loader: collect_skill_prompts failed: %s", exc)
        skill_prompts = []

    if skill_prompts:
        lines = ["\n\n## \u53ef\u7528\u6280\u80fd\uff08\u9700\u8981\u65f6\u7528 Read \u5de5\u5177\u8bfb\u53d6\u6307\u4ee4\u6587\u4ef6\uff09"]
        for sp in skill_prompts:
            lines.append(
                f"- {sp['name']}\uff1a{sp.get('description', '')}  \u2192 {sp['skill_md_abspath']}"
            )
        sys_prompt += "\n".join(lines)

    # ------------------------------------------------------------------
    # Build AgentState.
    #
    # Three paths:
    #   A) HITL resume: restore from saved agent_state dict passed by the
    #      orchestrator.
    #   B) HITL fresh run: DEFAULT permission mode + explicit ALLOW rules
    #      for every built-in native tool; MCP tools hit the default ASK
    #      path and trigger RequireUserConfirmEvent.
    #   C) Non-HITL (hitl=False): BYPASS mode.
    # ------------------------------------------------------------------
    agent_state = None

    if resume_payload is not None:
        # Path A -- HITL resume: restore AgentState from the saved dict.
        try:
            from agentscope.state import AgentState

            saved_state_dict = resume_payload.get("agent_state") or {}
            agent_state = AgentState.model_validate(saved_state_dict)
            logger.info(
                "HITL resume: restored AgentState (reply_id=%s, context_len=%d)",
                getattr(agent_state, "reply_id", "?"),
                len(getattr(agent_state, "context", [])),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "HITL resume: could not restore AgentState; "
                "falling back to fresh state: %s",
                exc,
            )
            resume_payload = None  # treat as fresh run

    if agent_state is None and hitl_enabled:
        # Path B -- HITL fresh run: DEFAULT permission mode + ALLOW rules
        # for built-in native tools.
        try:
            from agentscope.state import AgentState

            perm_ctx = _build_hitl_permission_context()
            if perm_ctx is not None:
                agent_state = AgentState(permission_context=perm_ctx)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "HITL: could not build AgentState with DEFAULT permission; "
                "falling back to BYPASS: %s",
                exc,
            )

    if agent_state is None:
        # Path C -- non-HITL (hitl=False) OR any HITL failure fallback.
        try:
            from agentscope.state import AgentState

            perm_ctx = _build_bypass_permission_context()
            if perm_ctx is not None:
                agent_state = AgentState(permission_context=perm_ctx)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "could not set BYPASS permission; tool calls may require "
                "confirmation: %s",
                exc,
            )

    # ------------------------------------------------------------------
    # Restore prior-turn conversation history (multi-turn memory).
    # Only on fresh runs -- resume runs already have context from the
    # restored AgentState.
    # ------------------------------------------------------------------
    # Resolve the agent name once, up-front: it must be identical for the
    # restored history Msgs *and* the Agent below.  agentscope keys context
    # ownership on ``msg.name == self.name`` (_get_last_msg / _save_to_context /
    # _update_tool_call_state in agentscope.agent._agent) — if restored
    # assistant/tool Msgs carry a different name than the agent, the agent does
    # not recognize them as its own: the first new reply block appends a fresh
    # AssistantMsg instead of merging, and tool-call-state updates targeting the
    # restored turn silently no-op.  Stamping restored assistant/tool Msgs with
    # the same ``agent_name`` keeps the restored turns owned by this agent.
    agent_name = str(task.get("agent_name") or "Worker")

    history = task.get("history") or []
    if resume_payload is None and agent_state is not None and isinstance(history, list):
        from agentscope.message import (
            Msg,
            TextBlock,
            ToolCallBlock,
            ToolResultBlock,
        )

        restored: list = []
        for item in history:
            try:
                role = item.get("role")
                if role == "user":
                    content_text = str(item.get("content") or "")
                    if content_text:
                        restored.append(
                            Msg(name="user", role="user",
                                content=[TextBlock(type="text", text=content_text)])
                        )
                elif role == "assistant":
                    content_text = str(item.get("content") or "")
                    if content_text:
                        restored.append(
                            Msg(name=agent_name, role="assistant",
                                content=[TextBlock(type="text", text=content_text)])
                        )
                elif role == "tool":
                    t = item.get("tool") or {}
                    cid = str(t.get("id") or "")
                    if not cid:
                        continue
                    tname = str(t.get("name") or "tool")
                    raw_input = t.get("input")
                    input_str = (
                        raw_input if isinstance(raw_input, str)
                        else json.dumps(raw_input or {}, ensure_ascii=False)
                    )
                    output_str = str(t.get("result") or "")
                    restored.append(
                        Msg(name=agent_name, role="assistant", content=[
                            ToolCallBlock(type="tool_call", id=cid,
                                          name=tname, input=input_str)
                        ])
                    )
                    restored.append(
                        Msg(name=agent_name, role="assistant", content=[
                            ToolResultBlock(type="tool_result", id=cid,
                                            name=tname, output=output_str)
                        ])
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning("skip restoring one history item: %s", exc)
        if restored:
            agent_state.context.extend(restored)
            logger.info("restored %d history messages into context", len(restored))

    agent = Agent(
        name=agent_name,
        system_prompt=sys_prompt,
        model=model,
        toolkit=toolkit,
        state=agent_state,
    )

    # Make sure events.jsonl exists even if the agent never emits.
    events_path = WORKSPACE / "events.jsonl"
    try:
        events_path.touch(exist_ok=True)
    except OSError:
        pass

    _hooks.attach_hooks(agent, events_path=events_path, env=env)

    # Mark run as RUNNING in Redis before dispatching (first write).
    state_client.write_step(plan="", cursor="start", status="RUNNING")

    # ------------------------------------------------------------------
    # Build the input for reply_stream:
    #   - Fresh run: plain text string (converted to Msg inside _dispatch).
    #   - HITL resume: appropriate confirm/answer event.
    # ------------------------------------------------------------------
    if resume_payload is not None:
        resume_kind = resume_payload.get("kind", "confirm")
        resume_reply_id = str(resume_payload.get("reply_id") or "")
        resume_tool_call_id = str(resume_payload.get("tool_call_id") or "")

        # Find the pending ToolCallBlock in the restored context.
        pending_tc = None
        try:
            for msg in reversed(list(agent_state.context)):  # type: ignore[union-attr]
                for block in getattr(msg, "content", []) or []:
                    if (
                        getattr(block, "type", None) == "tool_call"
                        and getattr(block, "id", None) == resume_tool_call_id
                    ):
                        pending_tc = block
                        break
                if pending_tc is not None:
                    break
        except Exception as exc:  # noqa: BLE001
            logger.warning("HITL resume: could not find pending ToolCallBlock: %s", exc)

        if resume_kind == "answer":
            # External tool (ask_user) result.
            try:
                from agentscope.event import ExternalExecutionResultEvent
                from agentscope.message import ToolResultBlock

                answer_text = str(resume_payload.get("answer") or "")
                result_block = ToolResultBlock(
                    type="tool_result",
                    id=resume_tool_call_id,
                    name="ask_user",
                    output=answer_text,
                )
                dispatch_input: object = ExternalExecutionResultEvent(
                    reply_id=resume_reply_id,
                    execution_results=[result_block],
                )
                logger.info(
                    "HITL resume: ExternalExecutionResultEvent tool_call_id=%s",
                    resume_tool_call_id,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "HITL resume: could not build ExternalExecutionResultEvent; "
                    "aborting: %s",
                    exc,
                )
                state_client.mark_failed()
                return "FAILED", f"HITL resume event build failed: {exc}", {}, []
        else:
            # MCP tool confirmation result.
            confirmed = bool(resume_payload.get("confirmed", True))
            try:
                from agentscope.event import UserConfirmResultEvent, ConfirmResult

                if pending_tc is None:
                    raise ValueError(
                        f"pending ToolCallBlock for id={resume_tool_call_id!r} "
                        "not found in restored context"
                    )
                confirm_result = ConfirmResult(
                    confirmed=confirmed,
                    tool_call=pending_tc,
                    rules=None,
                )
                dispatch_input = UserConfirmResultEvent(
                    reply_id=resume_reply_id,
                    confirm_results=[confirm_result],
                )
                logger.info(
                    "HITL resume: UserConfirmResultEvent tool_call_id=%s confirmed=%s",
                    resume_tool_call_id,
                    confirmed,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "HITL resume: could not build UserConfirmResultEvent; "
                    "aborting: %s",
                    exc,
                )
                state_client.mark_failed()
                return "FAILED", f"HITL resume event build failed: {exc}", {}, []
    else:
        # Fresh run: pass plain task text (converted to Msg inside _dispatch).
        dispatch_input = text

    # ------------------------------------------------------------------
    # Drive reply_stream.  A _PauseSignal means the run hit a HITL pause.
    # _handle_pause serialises the state and writes result.json with
    # status="paused"; we return a dedicated "paused" status tuple.
    #
    # IMPORTANT: a resumed run can pause AGAIN (second MCP call, second
    # ask_user).  The _PauseSignal catch is always reachable regardless of
    # whether this is a fresh or resume leg.
    # ------------------------------------------------------------------
    try:
        summary, usage, plan_steps = asyncio.run(
            _dispatch(agent, dispatch_input, events_path, state_client=state_client)
        )
        summary = summary.strip()
    except _PauseSignal as pause:
        # Failure-open: if _handle_pause itself errors, fall through to
        # the normal failure path.
        paused_ok = _handle_pause(
            event_type=pause.event_type,
            event=pause.event,
            agent=agent,
            run_id=run_id,
            state_client=state_client,
            usage=pause.usage,
            plan_steps=pause.plan_steps,
        )
        if paused_ok:
            return "paused", "(run paused for HITL confirmation)", pause.usage, pause.plan_steps
        # If _handle_pause failed, treat as a worker crash.
        state_client.mark_failed()
        return "FAILED", "HITL pause serialisation failed", pause.usage, pause.plan_steps
    except Exception:
        state_client.mark_failed()
        raise

    if not summary:
        summary = "(agent returned an empty reply)"

    state_client.mark_completed()
    return "OK", summary, usage, plan_steps



def main(argv: list[str] | None = None) -> int:
    del argv

    # Load scheduler-delivered secrets from /workspace/.secrets.json into
    # os.environ (userspace only) and unlink the file.  This MUST run before
    # the _require_env gate below: FOUNDATION_API_TOKEN now arrives via the
    # file, so it must be present in os.environ before the gate checks for it.
    _load_secret_file_into_env()

    # Fail loudly on missing context -- the scheduler should never spawn
    # a worker without these set.
    for var in (
        "FOUNDATION_API_URL",
        "FOUNDATION_API_TOKEN",
        "FOUNDATION_TENANT_ID",
        "FOUNDATION_USER_ID",
    ):
        try:
            _require_env(var)
        except ValueError as exc:
            return _exit(1, status="FAILED", summary="infra env missing", error=str(exc))

    try:
        task = _read_task()
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return _exit(2, status="FAILED", summary="invalid task.json", error=str(exc))

    try:
        status, summary, usage, plan_steps = _run_real(task)
    except ImportError as exc:
        return _exit(
            1,
            status="FAILED",
            summary="agentscope unavailable",
            error=f"{type(exc).__name__}: {exc}",
        )
    except Exception as exc:  # noqa: BLE001 -- last-resort safety net
        return _exit(
            1,
            status="FAILED",
            summary="worker crashed",
            error=f"{type(exc).__name__}: {exc}",
        )

    return _exit(0, status=status, summary=summary, usage=usage, plan_steps=plan_steps)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(sys.argv[1:]))
