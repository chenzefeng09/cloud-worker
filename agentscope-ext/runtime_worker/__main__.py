"""``python -m agentscope_ext.runtime_worker`` entrypoint.

Wire contract between agent-scheduler and the sandbox
-----------------------------------------------------

* **Env vars** (set by ``services/agent-scheduler/.../docker_runtime.py``
  and ``cube_runtime.py``):

  | var | meaning |
  |---|---|
  | ``FOUNDATION_API_URL``       | base URL of foundation services |
  | ``FOUNDATION_API_TOKEN``     | short-lived per-run JWT (≤ sandbox_timeout) |
  | ``FOUNDATION_TENANT_ID``     | for audit + LLM-gateway routing |
  | ``FOUNDATION_USER_ID``       | for audit attribution |
  | ``LLM_MODEL``                | which model AgentScope's ReActAgent uses |
  | ``OTEL_EXPORTER_OTLP_ENDPOINT`` | traces target |

* **Task delivery** (one of, in priority order):

  1. Env var ``AGENT_TASK_JSON`` — used by the CubeSandbox runtime, which
     can't bind-mount files.  Same JSON shape as the file.
  2. File ``/workspace/task.json`` — used by the docker runtime when the
     workspace dir is mounted.

  Shape (both): ``{"task": str, "mcp_servers": [...], "skills": [...]}``.

* **Result + events** (always written under ``/workspace``):

  | path | direction | shape |
  |---|---|---|
  | ``/workspace/result.json`` | out | ``{"status": "OK"|"FAILED", "summary": str, "error": str|null}`` |
  | ``/workspace/events.jsonl`` | out | one JSON event per line (MsgHub stream) |

* **Exit code** (read by ``DockerRuntime.get_run_status`` /
  ``CubeSandboxRuntime.get_run_status``):

  | code | meaning |
  |---|---|
  | 0   | task ran to completion (status in result.json) |
  | 1   | unrecoverable infra error (token expired, model unreachable) |
  | 2   | invalid task.json |
  | 137 | OOM-killed by sandbox |

Skeleton implementation
-----------------------
The B-M1 deliverable is the *contract*, not the working agent.  The body
below validates inputs, writes a placeholder ``result.json``, and exits
0 so end-to-end tests can flow through.  B-M2 swaps ``_run_skeleton``
for ``_run_real`` which actually builds a ``ReActAgent`` and dispatches.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

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
    """Resolve the task payload via env (cube) or file (docker).

    Either source is fine; we check env first because the cube runtime
    can't bind-mount files.  See the docstring at the top of this module.
    """
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


def _run_skeleton(task: dict) -> tuple[str, str]:
    """Stand-in for the real ReActAgent dispatch.

    Returns ``(status, summary)``.  B-M2 replaces this with::

        from agentscope.agents import ReActAgent
        from agentscope.toolkit import Toolkit
        ...
    """
    text = str(task.get("task", "")).strip()
    if not text:
        return "FAILED", "task text is empty"
    return "OK", f"[skeleton] echoed task ({len(text)} chars); B-M2 wires real ReActAgent"


def main(argv: list[str] | None = None) -> int:
    del argv  # B-M2: parse --task-file / --result-file overrides

    # Fail loudly on missing context — the scheduler should never spawn
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
        status, summary = _run_skeleton(task)
    except Exception as exc:  # pragma: no cover — last-resort safety net
        return _exit(1, status="FAILED", summary="worker crashed", error=str(exc))

    return _exit(0 if status == "OK" else 0, status=status, summary=summary)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(sys.argv[1:]))
