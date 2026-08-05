"""LLM-gateway enforcer: build an ``OpenAIChatModel`` pointed at the gateway.

Design rationale
----------------
The foundation ``llm-gateway`` exposes an OpenAI-compatible endpoint at::

    POST {LLM_GATEWAY_URL}/chat/completions
        (LLM_GATEWAY_URL is expected to include the /api/llm-gateway/v1 prefix)

Authentication uses two headers:

* ``Authorization: Bearer <FOUNDATION_API_TOKEN>`` — the per-run foundation JWT
  issued by the scheduler.
* ``X-LLM-Virtual-Key: <LLM_VIRTUAL_KEY>`` — the tenant virtual key that the
  gateway uses for quota accounting and model routing.

``OpenAIChatModel`` (agentscope 2.0) creates an ``openai.AsyncOpenAI`` client
internally in ``_call_api``.  The client constructor accepts
``default_headers`` but ``OpenAIChatModel`` does not expose it.  We subclass
``OpenAIChatModel`` and override ``_call_api`` to inject the gateway headers.
This preserves the full native streaming / tool-call / formatter pipeline —
delta events, ``ToolCallBlock``, ``ThinkingBlock`` — without any hand-rolled
round-trip.

The ``OpenAICredential.api_key`` is set to the virtual key (openai-sdk requires
a non-empty API key string).  The gateway also accepts the virtual key via
``Authorization`` when ``X-LLM-Virtual-Key`` is absent, so the two auth paths
are compatible.

``FOUNDATION_API_TOKEN`` doubles as the bearer token; it is always present
inside the sandbox (injected by the scheduler).  ``LLM_VIRTUAL_KEY`` is the
new env var injected by M1-P3/P4 when the scheduler resolves and injects the
tenant virtual key.  If it is absent we fall back to using the virtual key =
the bearer token (so the module stays importable and callable even without the
new scheduler wiring).

``make_gateway_model`` is the public factory; ``_GatewayCallable`` and the old
``ChatResponse`` shim are **kept** for import-compatibility with existing unit
tests but are no longer used in production.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator, Mapping

_LOG = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Legacy shim (kept for backwards-compat with existing tests)
# ---------------------------------------------------------------------------


@dataclass
class ChatResponse:
    """Minimal response shim — used only by the old-style _GatewayCallable tests."""

    content: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    raw: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Module-level httpx client hook (used only by legacy _GatewayCallable tests)
# ---------------------------------------------------------------------------

try:
    import httpx as _httpx

    _DEFAULT_TIMEOUT = _httpx.Timeout(60.0, connect=5.0)
    _CLIENT: "_httpx.Client | None" = None

    def set_client(client: "_httpx.Client | None") -> None:
        """Override the module-level ``httpx.Client`` (mainly for tests)."""
        global _CLIENT
        _CLIENT = client

    def _client() -> "_httpx.Client":
        global _CLIENT
        if _CLIENT is None:
            _CLIENT = _httpx.Client(timeout=_DEFAULT_TIMEOUT)
        return _CLIENT

except ImportError:  # pragma: no cover

    def set_client(client: Any) -> None:  # type: ignore[misc]
        pass

    def _client() -> Any:  # type: ignore[misc]
        raise RuntimeError("httpx not available")


# ---------------------------------------------------------------------------
# Gateway-aware OpenAIChatModel subclass
# ---------------------------------------------------------------------------


def _build_openai_gateway_model(
    *,
    gateway_url: str,
    virtual_key: str,
    bearer_token: str,
    model_name: str,
    formatter: Any,
    tenant_id: str = "",
    user_id: str = "",
    broker_lease: str = "",
    reasoning_effort: str | None = None,
    sampling_params: Mapping[str, Any] | None = None,
) -> Any:
    """Return a ``_GatewayOpenAIChatModel`` that talks to the foundation gateway.

    The subclass overrides ``_call_api`` to inject the two auth headers that
    the gateway requires:

    * ``Authorization: Bearer <bearer_token>``  — the sandbox-lifetime JWT
    * ``X-LLM-Virtual-Key: <virtual_key>``      — tenant quota/routing key

    Args:
        gateway_url: Full base URL for the gateway, e.g.
            ``http://llm-gw:8011/api/llm-gateway/v1``.  Must NOT end with
            ``/chat/completions``; the openai SDK appends that itself.
        virtual_key: Tenant virtual key.  Also used as ``api_key`` for the
            openai client (which requires a non-empty string).
        bearer_token: Per-run foundation JWT.
        model_name: Model routing key passed as ``model`` to the gateway.
        formatter: AgentScope formatter instance (``OpenAIChatFormatter``).

    Returns:
        A ``ChatModelBase`` subclass instance ready to be plugged into
        ``Agent(model=...)``.
    """
    from agentscope.credential import OpenAICredential
    from agentscope.model import OpenAIChatModel

    credential = OpenAICredential(
        api_key=virtual_key or bearer_token or "gateway-placeholder",
        base_url=gateway_url,
    )

    extra_headers: dict[str, str] = {
        "Authorization": f"Bearer {bearer_token}",
    }
    if virtual_key:
        extra_headers["X-LLM-Virtual-Key"] = virtual_key
    # The gateway's TenantContextMiddleware resolves the tenant from the
    # ``X-Tenant-Id`` header and returns 401 ("Tenant context missing") if it
    # is absent — the Bearer token is NOT yet JWT-validated for a tenant claim.
    # The scheduler injects FOUNDATION_TENANT_ID into every sandbox, so we
    # forward it here.  Without this header the enforcer cannot reach the
    # gateway at all.
    if tenant_id:
        extra_headers["X-Tenant-Id"] = tenant_id
    if user_id:
        extra_headers["X-User-Id"] = user_id
    if broker_lease:
        extra_headers["X-Tool-Broker-Lease"] = broker_lease

    class _GatewayOpenAIChatModel(OpenAIChatModel):
        """OpenAIChatModel subclass that injects gateway auth headers."""

        async def _call_api(
            self,
            model_name: str,
            messages: list,
            tools: list[dict] | None = None,
            tool_choice: Any | None = None,
            **generate_kwargs: Any,
        ) -> "Any | AsyncGenerator":
            # Re-enter with the bound closure variables so the overriding
            # happens transparently.  We import openai here (same place as
            # the parent does) so the dependency stays optional at module
            # import time.
            import openai
            from datetime import datetime

            client = openai.AsyncOpenAI(
                api_key=self.credential.api_key.get_secret_value(),
                base_url=self.credential.base_url,
                default_headers=extra_headers,
            )

            formatted_messages = await self.formatter.format(messages)

            kwargs: dict[str, Any] = {
                "model": model_name,
                "messages": formatted_messages,
                "stream": self.stream,
            }

            # Per-run sampling overrides (App harness, task.json["sampling_params"])
            # take precedence over the model's own ``self.parameters.*`` defaults.
            # Each kwarg is set only when the resolved value is not None, so chat
            # runs (empty ``sampling``) preserve the prior behaviour exactly.
            _sampling = sampling_params or {}
            max_tokens = _sampling.get("max_tokens", self.parameters.max_tokens)
            temperature = _sampling.get("temperature", self.parameters.temperature)
            top_p = _sampling.get("top_p", self.parameters.top_p)
            if max_tokens is not None:
                kwargs["max_tokens"] = max_tokens
            if temperature is not None:
                kwargs["temperature"] = temperature
            if top_p is not None:
                kwargs["top_p"] = top_p
            # Per-message reasoning effort (DeepSeek/V4): the gateway upstream
            # requires BOTH the ``reasoning_effort`` field AND the ``extra_body``
            # thinking flag to enter thinking mode. When effort is absent we send
            # neither (non-thinking mode). The closure ``reasoning_effort`` is the
            # per-run value resolved from task.json; we fall back to the model's
            # own parameters for callers that configure it that way.
            effort = reasoning_effort or (
                self.parameters.reasoning_effort
                if self.parameters.thinking_enable
                else None
            )
            if effort:
                kwargs["reasoning_effort"] = effort
                extra_body = dict(kwargs.get("extra_body") or {})
                extra_body["thinking"] = {"type": "enabled"}
                kwargs["extra_body"] = extra_body

            kwargs.update(generate_kwargs)

            fmt_tools, fmt_tool_choice = self._format_tools(tools, tool_choice)
            if fmt_tools:
                kwargs["tools"] = fmt_tools
                if not self.parameters.parallel_tool_calls:
                    kwargs["parallel_tool_calls"] = False
            if fmt_tool_choice is not None:
                kwargs["tool_choice"] = fmt_tool_choice

            if self.stream:
                kwargs["stream_options"] = {"include_usage": True}

            start_datetime = datetime.now()
            response = await client.chat.completions.create(**kwargs)

            audio_cfg = kwargs.get("audio")
            audio_fmt = (
                audio_cfg.get("format", "wav")
                if isinstance(audio_cfg, dict)
                else "wav"
            )

            # agentscope 2.0.4 dropped the ``audio_fmt`` parameter from both
            # parse helpers (older/newer releases accept it).  Adapt to the
            # installed signature instead of hard-coding one — the bootstrap
            # image pins 2.0.4, where the 3-arg call crashes every streaming
            # run with "takes 3 positional arguments but 4 were given".
            import inspect as _inspect

            def _parse_args(fn: Any) -> tuple:
                params = _inspect.signature(fn).parameters
                if "audio_fmt" in params or len(params) >= 3:
                    return (start_datetime, response, audio_fmt)
                return (start_datetime, response)

            if self.stream:
                parse = self._parse_stream_response
                return parse(*_parse_args(parse))

            parse = self._parse_completion_response
            return parse(*_parse_args(parse))

    return _GatewayOpenAIChatModel(
        credential=credential,
        model=model_name,
        stream=True,
        formatter=formatter,
    )


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------


def make_gateway_model(*, task: dict, env: Mapping[str, str]) -> Any:
    """Return a gateway-wired ``OpenAIChatModel`` subclass instance.

    Reads the following env vars (all injected by the scheduler):

    * ``LLM_GATEWAY_URL``      — full gateway base URL including /api/llm-gateway/v1
    * ``LLM_VIRTUAL_KEY``      — tenant virtual key (optional; falls back to bearer)
    * ``FOUNDATION_API_TOKEN`` — per-run sandbox JWT used as bearer token
    * ``LLM_MODEL``            — model routing key

    Validation
    ----------
    * Raises ``ValueError`` if ``LLM_GATEWAY_URL`` is not set (we need an
      explicit gateway URL, not a guess).
    * Raises ``ValueError`` if ``LLM_MODEL`` is not set.
    * ``LLM_VIRTUAL_KEY`` is optional — if absent, the bearer token is used as
      the api_key placeholder (network auth still works via ``Authorization``).
    * ``FOUNDATION_API_TOKEN`` absent is non-fatal (yields an empty bearer);
      the gateway will 401 — but this matches the normal infra failure mode.

    Returns
    -------
    An ``OpenAIChatModel`` subclass instance when agentscope + openai are
    importable, otherwise raises ``ImportError``.
    """
    # Per-message overrides resolved from task.json (written by the scheduler).
    # The model override wins over the env default; reasoning_effort drives the
    # gateway thinking flags. Both may be absent (defaults / non-thinking).
    model_override = (task or {}).get("model_override")
    reasoning_effort = (task or {}).get("reasoning_effort") or None
    # App-harness per-run sampling overrides (temperature/top_p/max_tokens).
    # Absent/empty for chat runs, in which case the model defaults apply.
    sampling_params_raw = (task or {}).get("sampling_params")
    sampling_params = (
        sampling_params_raw if isinstance(sampling_params_raw, dict) else {}
    )

    gateway_url = env.get("LLM_GATEWAY_URL", "").rstrip("/")
    if not gateway_url:
        raise ValueError(
            "LLM_GATEWAY_URL is required for llm_gateway_enforcer; "
            "set it to the gateway base URL (e.g. http://llm-gw:8011/api/llm-gateway/v1)"
        )

    model_name = (model_override or env.get("LLM_MODEL", "")).strip()
    if not model_name:
        raise ValueError("LLM_MODEL is required for llm_gateway_enforcer")

    virtual_key = env.get("LLM_VIRTUAL_KEY", "").strip()
    bearer_token = env.get("FOUNDATION_API_TOKEN", "").strip()
    tenant_id = env.get("FOUNDATION_TENANT_ID", "").strip()
    user_id = env.get("FOUNDATION_USER_ID", "").strip()
    broker_lease = env.get("TOOL_BROKER_LEASE", "").strip()

    # Build formatter (OpenAI-compatible; gateway speaks OpenAI wire format).
    try:
        from agentscope.formatter import OpenAIChatFormatter

        formatter = OpenAIChatFormatter()
    except ImportError as exc:
        raise ImportError(
            "agentscope.formatter is required for llm_gateway_enforcer"
        ) from exc

    _LOG.info(
        "llm_gateway_enforcer: routing via %s (model=%s, virtual_key=%s)",
        gateway_url,
        model_name,
        "set" if virtual_key else "absent-using-bearer",
    )

    return _build_openai_gateway_model(
        gateway_url=gateway_url,
        virtual_key=virtual_key,
        bearer_token=bearer_token,
        model_name=model_name,
        formatter=formatter,
        tenant_id=tenant_id,
        user_id=user_id,
        broker_lease=broker_lease,
        reasoning_effort=reasoning_effort,
        sampling_params=sampling_params,
    )


__all__ = ["ChatResponse", "make_gateway_model", "set_client"]
