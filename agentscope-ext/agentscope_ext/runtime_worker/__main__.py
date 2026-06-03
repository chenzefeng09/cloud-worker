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
  | ``/workspace/result.json`` | out | ``{"status": "OK"|"FAILED", "summary": str, "error": str|null}`` |
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
import json
import logging
import os
import sys
from pathlib import Path

from agentscope_ext.runtime_worker import hooks as _hooks
from agentscope_ext.runtime_worker import redis_state as _redis

WORKSPACE = Path("/workspace")


def _exit(code: int, *, status: str, summary: str, error: str | None = None) -> int:
    """Write result.json + return the exit code; never raises."""
    try:
        (WORKSPACE / "result.json").write_text(
            json.dumps(
                {"status": status, "summary": summary, "error": error},
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


async def _dispatch(
    agent: object,
    task_text: str,
    events_path: Path,
    state_client: "_redis.RunStateClient | None" = None,
) -> str:
    """Call the agent with the task text and return the final reply text.

    We use ``reply_stream`` so every event lands in ``events.jsonl``;
    AgentScope 2.0's ``ReplyEndEvent`` is just a marker (no Msg attached),
    so we reconstruct the assistant text by accumulating
    ``TextBlockDeltaEvent.delta`` chunks per block id.

    ``state_client`` is optional: when provided, hot-state is written to
    Redis on every event so a restart can detect the last checkpoint.
    """
    from agentscope.message import Msg, TextBlock

    user_msg = Msg(
        name="user",
        content=[TextBlock(type="text", text=task_text)],
        role="user",
    )

    # Accumulate delta chunks per text-block id; preserve insertion order
    # so concurrent blocks come out in stream order.
    text_blocks: dict[str, str] = {}
    # Track last plan text (from ThinkEvent) so we can surface it in state.
    last_plan: str = ""

    async for event in agent.reply_stream(user_msg):
        _hooks.write_event(events_path, event)
        event_type = type(event).__name__

        # Extract plan text from ThinkEvent (AgentScope 2.x reasoning event).
        if event_type == "ThinkEvent":
            thought = getattr(event, "thought", None) or getattr(event, "text", None) or ""
            if thought:
                last_plan = str(thought)

        if event_type == "TextBlockStartEvent":
            block_id = getattr(event, "block_id", None)
            if block_id and block_id not in text_blocks:
                text_blocks[block_id] = ""
        elif event_type == "TextBlockDeltaEvent":
            block_id = getattr(event, "block_id", None) or ""
            delta = getattr(event, "delta", "") or ""
            text_blocks[block_id] = text_blocks.get(block_id, "") + str(delta)

        # Write hot state after every event (failure-open).
        if state_client is not None:
            state_client.write_step(plan=last_plan, cursor=event_type)

    return "\n".join(t for t in text_blocks.values() if t).strip()


def _s3_client(env: dict):
    """Build a boto3 S3 client from the S3_* env vars.

    Returns ``None`` (failure-open) if boto3 is unavailable or credentials
    are missing.  Callers must guard with ``if client is None: return ...``.

    We always pass the S3_* credentials explicitly because boto3 defaults to
    looking for AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY; those names are
    NOT injected into the sandbox, so relying on boto3's auto-discovery would
    silently produce an unauthenticated client that fails every call.
    """
    import warnings

    endpoint_url = env.get("S3_ENDPOINT_URL") or None
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


def _run_real(task: dict) -> tuple[str, str]:
    """Build an Agent from the configured hooks and run the task.

    Returns ``(status, summary)``.  Raises only for unrecoverable
    infra failures (caught in :func:`main` and surfaced as exit-1).
    """
    logger = logging.getLogger(__name__)

    text = str(task.get("task", "")).strip()
    if not text:
        return "FAILED", "task text is empty"

    # Local imports so this module is importable without ``agentscope``;
    # the ImportError flows up to ``main`` and turns into exit code 1.
    from agentscope.agent import Agent

    env = _hooks.snapshot_env()
    tenant_id = env.get("FOUNDATION_TENANT_ID", "")

    # ------------------------------------------------------------------
    # Hot-state: connect to Redis and detect checkpoint from a prior run.
    # failure-open — RunStateClient swallows all Redis errors internally.
    # ------------------------------------------------------------------
    state_client = _redis.from_env()

    checkpoint = state_client.read_checkpoint()
    if checkpoint is not None and checkpoint.get("status") not in ("COMPLETED", "FAILED"):
        prior_step = checkpoint.get("step", 0)
        prior_plan = checkpoint.get("plan", "")
        logger.info(
            "resuming from step %d (prior plan: %.120s...)",
            prior_step,
            prior_plan,
        )
        # Inject resume context into task so the agent knows it is restarting.
        # We surface this via the memory field so it reaches the system prompt
        # through the existing memory-injection path in _load_prompt_files.
        existing_memory = str(task.get("memory") or "").strip()
        resume_note = (
            f"[系统：本次运行是从断点恢复，上次已完成 {prior_step} 步。"
            + (f"上次计划：{prior_plan}" if prior_plan else "")
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

    # Build system prompt: soul/identity/user files from S3 + task-level override.
    sys_prompt = _load_prompt_files(env, task)

    # Inject long-term memory snippets (pre-fetched by orchestrator, stored in
    # task.json["memory"]).  These arrive as a plain string already formatted
    # by the orchestrator; we prepend them so the agent sees them before the
    # main instructions.
    memory_snippets = str(task.get("memory") or "").strip()
    if memory_snippets:
        sys_prompt = f"## 相关历史记忆\n\n{memory_snippets}\n\n---\n\n{sys_prompt}"

    # Append uploaded-file context if any were downloaded.
    if downloaded:
        file_list = ", ".join(downloaded)
        sys_prompt += (
            f"\n\n用户上传了以下文件，位于 /workspace/inputs/：{file_list}\n"
            "你可以读取这些文件来完成任务。"
        )

    agent = Agent(
        name=str(task.get("agent_name") or "Worker"),
        system_prompt=sys_prompt,
        model=model,
        toolkit=toolkit,
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

    try:
        summary = asyncio.run(
            _dispatch(agent, text, events_path, state_client=state_client)
        ).strip()
    except Exception:
        state_client.mark_failed()
        raise

    if not summary:
        summary = "(agent returned an empty reply)"

    state_client.mark_completed()
    return "OK", summary


def main(argv: list[str] | None = None) -> int:
    del argv

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
        status, summary = _run_real(task)
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

    return _exit(0, status=status, summary=summary)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(sys.argv[1:]))
