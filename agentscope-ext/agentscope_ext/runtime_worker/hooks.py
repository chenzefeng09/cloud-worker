"""Extension hooks for ``runtime_worker`` (AgentScope 2.0).

The runtime worker is intentionally minimal — it knows how to read a task,
instantiate a single ``Agent``, run it, and persist the outcome.  The
*interesting* behaviour (tenant-scoped MCP, gateway-routed LLM, audit
forwarding, packaged Skills) lives in sibling ``agentscope_ext.*`` packages
that monkey-patch the four module-level functions defined here.

Override contract
-----------------

Each function below is looked up via attribute access on this module at
agent-build time, so an extension may replace any of them by doing::

    from agentscope_ext.runtime_worker import hooks

    def _build_model_with_gateway(task, env):
        from agentscope_ext.llm_gateway_enforcer import GatewayModel
        return GatewayModel(env=env)

    hooks.build_model = _build_model_with_gateway

The four hook points map 1:1 onto the planned sibling packages:

==================  ====================================================
Hook                Override owner (planned)
==================  ====================================================
``build_model``     ``agentscope_ext.llm_gateway_enforcer``
``build_formatter`` ``agentscope_ext.llm_gateway_enforcer`` (paired with
                    the model — both follow the chosen provider)
``build_toolkit``   ``agentscope_ext.mcp_tenant_injector`` and
                    ``agentscope_ext.skills_loader`` (chained: MCP first,
                    Skills layered on top)
``attach_hooks``    ``agentscope_ext.event_audit_sink``
==================  ====================================================

Signatures
----------

* ``build_model(task: dict, env: Mapping[str, str]) -> ChatModelBase``
* ``build_formatter(task: dict, env: Mapping[str, str]) -> FormatterBase``
* ``build_toolkit(task: dict, tenant_id: str, env: Mapping[str, str]) -> Toolkit``
* ``attach_hooks(agent, *, events_path: pathlib.Path, env: Mapping[str, str]) -> None``

The defaults below give you a working solo-agent loop with **no** external
dependencies beyond AgentScope itself — useful for local smoke testing
without the rest of the foundation stack running.

Failure mode
------------

AgentScope imports are deferred to call time so this module is importable
(and unit-testable) even when ``agentscope`` is not installed.  Each hook
raises ``ImportError`` with a clear message in that case; the
``runtime_worker`` entrypoint catches and surfaces it via ``result.json``
with exit code 1.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping


# ---------------------------------------------------------------------------
# Model + formatter
# ---------------------------------------------------------------------------


def build_model(task: dict, env: Mapping[str, str]) -> Any:
    """Default model: Anthropic / OpenAI-compatible / DeepSeek based on ``LLM_PROVIDER``.

    ``LLM_PROVIDER`` values:

    * ``anthropic`` (default) -- ``agentscope.model.AnthropicChatModel``
    * ``openai``              -- ``agentscope.model.OpenAIChatModel`` (also
      used for any OpenAI-compatible endpoint, e.g. vLLM, DeepSeek, or
      an in-house gateway, by setting ``LLM_BASE_URL``)
    * ``deepseek``            -- ``agentscope.model.DeepSeekChatModel``
      (native DeepSeek client; ``LLM_BASE_URL`` optional)

    ``LLM_MODEL`` is the model name; ``LLM_API_KEY`` is the credential.
    The llm-gateway-enforcer extension is expected to override this and
    ignore the raw key -- it talks to ``foundation.llm-gateway`` instead.
    """
    provider = env.get("LLM_PROVIDER", "anthropic").lower()
    model_name = env.get("LLM_MODEL") or _provider_default_model(provider)
    api_key = env.get("LLM_API_KEY", "")
    base_url = env.get("LLM_BASE_URL", "") or None
    formatter = build_formatter(task, env)

    if provider == "anthropic":
        from agentscope.credential import AnthropicCredential
        from agentscope.model import AnthropicChatModel

        cred_kwargs: dict[str, Any] = {"api_key": api_key}
        if base_url:
            cred_kwargs["base_url"] = base_url
        return AnthropicChatModel(
            credential=AnthropicCredential(**cred_kwargs),
            model=model_name,
            stream=False,
            formatter=formatter,
        )

    if provider == "openai":
        from agentscope.credential import OpenAICredential
        from agentscope.model import OpenAIChatModel

        cred_kwargs = {"api_key": api_key}
        if base_url:
            cred_kwargs["base_url"] = base_url
        return OpenAIChatModel(
            credential=OpenAICredential(**cred_kwargs),
            model=model_name,
            stream=False,
            formatter=formatter,
        )

    if provider == "deepseek":
        from agentscope.credential import DeepSeekCredential
        from agentscope.model import DeepSeekChatModel

        cred_kwargs = {"api_key": api_key}
        if base_url:
            cred_kwargs["base_url"] = base_url
        return DeepSeekChatModel(
            credential=DeepSeekCredential(**cred_kwargs),
            model=model_name,
            stream=False,
            formatter=formatter,
        )

    raise ValueError(f"unsupported LLM_PROVIDER: {provider!r}")


def build_formatter(task: dict, env: Mapping[str, str]) -> Any:
    """Default formatter — must match whichever provider ``build_model`` picked."""
    del task
    provider = env.get("LLM_PROVIDER", "anthropic").lower()
    if provider == "anthropic":
        from agentscope.formatter import AnthropicChatFormatter

        return AnthropicChatFormatter()

    if provider == "openai":
        from agentscope.formatter import OpenAIChatFormatter

        return OpenAIChatFormatter()

    if provider == "deepseek":
        from agentscope.formatter import DeepSeekChatFormatter

        return DeepSeekChatFormatter()

    raise ValueError(f"unsupported LLM_PROVIDER: {provider!r}")


def _provider_default_model(provider: str) -> str:
    if provider == "anthropic":
        return "claude-3-5-sonnet-latest"
    if provider == "openai":
        return "gpt-4o-mini"
    if provider == "deepseek":
        return "deepseek-chat"
    return ""


# ---------------------------------------------------------------------------
# Toolkit
# ---------------------------------------------------------------------------


def build_toolkit(task: dict, tenant_id: str, env: Mapping[str, str]) -> Any:
    """Default toolkit — pre-loaded with built-in planning tools.

    ``TaskCreate``, ``TaskUpdate``, and ``TaskList`` are always registered so
    the agent can emit structured plan steps that are read by the worker and
    streamed to the frontend PLAN panel in real time.

    The ``mcp_tenant_injector`` extension is expected to resolve the
    tenant's MCP server list from ``foundation.mcp-registry`` and feed
    the resulting MCP clients into the returned Toolkit (appending to the
    basic group's mcps list — it does NOT replace the Toolkit, so the
    planning tools are preserved).  The ``skills_loader`` extension layers
    package-defined Skills on top via the same append pattern.

    Returning a toolkit with the planning tools by default keeps the worker
    runnable in smoke tests where no foundation services exist.
    """
    del task, tenant_id, env  # arguments are part of the override contract
    from agentscope.tool import Toolkit, TaskCreate, TaskUpdate, TaskList

    return Toolkit(tools=[TaskCreate(), TaskUpdate(), TaskList()])


# ---------------------------------------------------------------------------
# Hooks (event streaming)
# ---------------------------------------------------------------------------


def attach_hooks(agent: Any, *, events_path: Path, env: Mapping[str, str]) -> None:
    """Default ``attach_hooks`` is a no-op in AgentScope 2.0.

    AgentScope 2.0 dropped the ``register_instance_hook`` API; observability
    is now done by consuming ``Agent.reply_stream`` events at the caller.
    The ``__main__`` entrypoint writes each streamed event into
    ``events.jsonl`` itself, so this default has nothing to wire up.

    The ``event_audit_sink`` extension is expected to override this hook
    to additionally POST each event to ``foundation.audit``.  Keeping the
    hook in the public surface preserves the override contract used by
    sibling extensions.
    """
    del agent, events_path, env  # arguments are part of the override contract


# ---------------------------------------------------------------------------
# Convenience for tests / extensions
# ---------------------------------------------------------------------------


def snapshot_env() -> dict[str, str]:
    """Return a copy of the env vars the hooks care about.

    Extensions and tests use this to avoid mutating ``os.environ`` and
    to make the call sites' inputs explicit.
    """
    keys = (
        "LLM_PROVIDER",
        "LLM_MODEL",
        "LLM_API_KEY",
        "LLM_BASE_URL",
        "FOUNDATION_API_URL",
        "FOUNDATION_API_TOKEN",
        "FOUNDATION_TENANT_ID",
        "FOUNDATION_USER_ID",
        # B-M3: dedicated service URLs + service scopes injected by the
        # scheduler so mcp_tenant_injector / skills_loader can reach the
        # mcp-registry / skill-store internal endpoints (instead of falling
        # back to FOUNDATION_API_URL, which points at Keycloak in dev).
        "MCP_REGISTRY_URL",
        "SKILL_STORE_URL",
        "FOUNDATION_SERVICE_SCOPES",
        "RUNTIME_EXTENSIONS",
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        # S3/MinIO — for downloading user-uploaded attachments in runtime_worker.
        "S3_ENDPOINT_URL",
        "S3_BUCKET",
        "S3_REGION",
        "S3_ACCESS_KEY_ID",
        "S3_SECRET_ACCESS_KEY",
        # Hot-state layer — Redis URL + stable run identifier.
        # Injected by the scheduler's docker_runtime.py so the in-sandbox
        # worker can write step/plan/cursor to Redis for breakpoint resume.
        "REDIS_URL",
        "RUN_ID",
    )
    return {k: os.environ[k] for k in keys if k in os.environ}


# ---------------------------------------------------------------------------
# Helper used by the entrypoint to persist reply_stream events.
# Kept here so extensions can wrap it (e.g. to also POST to audit).
# ---------------------------------------------------------------------------


def write_event(events_path: Path, event: Any) -> None:
    """Append one AgentScope reply_stream event to ``events.jsonl``."""
    try:
        if hasattr(event, "model_dump"):
            payload = event.model_dump()
            payload.setdefault("event_type", type(event).__name__)
        elif isinstance(event, dict):
            payload = dict(event)
        else:
            payload = {"event_type": type(event).__name__, "repr": repr(event)}
    except Exception as exc:  # pragma: no cover - defensive
        payload = {"error": f"serialise failed: {exc!r}"}
    try:
        with events_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
    except OSError:
        # Disk full / read-only mount -- drop the event rather than crash.
        pass
