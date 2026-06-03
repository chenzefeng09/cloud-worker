"""Custom ``ModelBase`` that proxies through ``foundation.llm-gateway``.

The implementation is split in two layers so it can be unit-tested
without AgentScope installed:

* :class:`_GatewayCallable` — a plain ``async`` callable that takes
  AgentScope-style messages, builds the OpenAI-compatible chat
  request, POSTs it through ``httpx``, and adapts the response.

* :class:`GatewayModel` (created lazily inside :func:`make_gateway_model`)
  — the real AgentScope ``ChatModelBase`` subclass.  It composes the
  callable above and lives behind a factory so importing this module
  does not require AgentScope.

The metadata ``{"tenant_id", "user_id"}`` injected on every request is
what ``llm-gateway`` uses for per-tenant rate limits and billing.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

import httpx

_LOG = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = httpx.Timeout(60.0, connect=5.0)
_CLIENT: httpx.Client | None = None


def set_client(client: httpx.Client | None) -> None:
    """Override the module-level ``httpx.Client`` (mainly for tests)."""
    global _CLIENT
    _CLIENT = client


def _client() -> httpx.Client:
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = httpx.Client(timeout=_DEFAULT_TIMEOUT)
    return _CLIENT


@dataclass
class ChatResponse:
    """Minimal AgentScope-compatible response surface.

    AgentScope's ``ChatResponse`` carries more fields (usage, raw
    response, etc.); the runtime worker only inspects ``content`` and
    ``tool_calls`` so we keep this lightweight.  When AgentScope's
    real class is importable we wrap *its* type instead — see
    :func:`_to_agentscope_response`.
    """

    content: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    raw: dict[str, Any] | None = None


class _GatewayCallable:
    """The plain-Python implementation of the gateway round-trip."""

    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        model_name: str,
        tenant_id: str,
        user_id: str,
    ) -> None:
        if not base_url:
            raise ValueError("FOUNDATION_API_URL is required for GatewayModel")
        if not model_name:
            raise ValueError("LLM_MODEL is required for GatewayModel")
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.model_name = model_name
        self.tenant_id = tenant_id
        self.user_id = user_id

    @property
    def endpoint(self) -> str:
        return f"{self.base_url}/llm-gateway/v1/chat/completions"

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def build_payload(
        self,
        messages: Iterable[Any],
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Translate AgentScope/OpenAI-ish messages into the gateway body."""
        oai_messages = [_to_openai_message(m) for m in messages]
        payload: dict[str, Any] = {
            "model": self.model_name,
            "messages": oai_messages,
            "metadata": {
                "tenant_id": self.tenant_id,
                "user_id": self.user_id,
            },
        }
        # Passthrough commonly-used knobs.
        for key in ("temperature", "top_p", "max_tokens", "stop", "stream", "tools"):
            if key in kwargs and kwargs[key] is not None:
                payload[key] = kwargs[key]
        return payload

    def _post(self, payload: dict[str, Any]) -> httpx.Response:
        response = _client().post(self.endpoint, json=payload, headers=self._headers())
        response.raise_for_status()
        return response

    def call_sync(self, messages: Iterable[Any], **kwargs: Any) -> ChatResponse:
        payload = self.build_payload(messages, **kwargs)
        response = self._post(payload)
        return _parse_response(response.json())

    async def __call__(self, messages: Iterable[Any], **kwargs: Any) -> ChatResponse:
        # The shared client is sync — POSTs to the gateway are cheap and we
        # currently run one agent per process.  If the worker ever moves to
        # a concurrent loop this should switch to ``httpx.AsyncClient``.
        return self.call_sync(messages, **kwargs)


def _to_openai_message(msg: Any) -> dict[str, Any]:
    """Best-effort conversion of an AgentScope ``Msg`` (or dict) -> OpenAI shape."""
    if isinstance(msg, Mapping):
        role = msg.get("role") or msg.get("name") or "user"
        content = msg.get("content", "")
    else:
        role = getattr(msg, "role", None) or getattr(msg, "name", None) or "user"
        content = getattr(msg, "content", "")
    if isinstance(content, list):
        # Flatten AgentScope content blocks into a plain string for the
        # gateway's OpenAI surface; richer block support is a follow-up.
        parts: list[str] = []
        for block in content:
            if isinstance(block, Mapping):
                if block.get("type") == "text":
                    parts.append(str(block.get("text", "")))
                else:
                    parts.append(json.dumps(block, ensure_ascii=False))
            else:
                parts.append(str(block))
        content = "\n".join(p for p in parts if p)
    elif not isinstance(content, str):
        content = str(content)
    return {"role": str(role), "content": content}


def _parse_response(body: dict[str, Any]) -> ChatResponse:
    """Extract the assistant message from an OpenAI-shaped response."""
    try:
        choice = body["choices"][0]
        message = choice.get("message") or {}
    except (KeyError, IndexError, TypeError):
        return ChatResponse(content="", tool_calls=[], raw=body)
    content = message.get("content") or ""
    tool_calls = list(message.get("tool_calls") or [])
    return ChatResponse(content=str(content), tool_calls=tool_calls, raw=body)


# ---------------------------------------------------------------------------
# AgentScope-facing factory
# ---------------------------------------------------------------------------


def make_gateway_model(*, task: dict, env: Mapping[str, str]) -> Any:
    """Return a GatewayModel instance bound to ``env``.

    If AgentScope is importable we return a real ``ChatModelBase``
    subclass so ``ReActAgent`` accepts it without further adaption; if
    not (unit tests for sibling extensions), we return the plain
    callable so the caller can still drive it.
    """
    del task
    callable_ = _GatewayCallable(
        base_url=env.get("FOUNDATION_API_URL", ""),
        token=env.get("FOUNDATION_API_TOKEN", ""),
        model_name=env.get("LLM_MODEL", ""),
        tenant_id=env.get("FOUNDATION_TENANT_ID", ""),
        user_id=env.get("FOUNDATION_USER_ID", ""),
    )

    base_cls = _try_agentscope_chat_model_base()
    if base_cls is None:
        return callable_

    class GatewayModel(base_cls):  # type: ignore[misc, valid-type]
        """ChatModelBase subclass that delegates to the gateway."""

        def __init__(self) -> None:
            try:
                super().__init__(model_name=callable_.model_name, stream=False)
            except TypeError:
                # Older / different ChatModelBase signatures — keep going.
                super().__init__()
            self._gateway = callable_

        async def __call__(self, messages: Any, **kwargs: Any) -> Any:
            return await self._gateway(messages, **kwargs)

    return GatewayModel()


def _try_agentscope_chat_model_base() -> type | None:
    """Locate ``ChatModelBase`` (name varies across AgentScope minors)."""
    try:
        from agentscope.model import ChatModelBase  # type: ignore[attr-defined]

        return ChatModelBase
    except (ImportError, AttributeError):
        pass
    try:
        from agentscope.model import ModelBase  # type: ignore[attr-defined]

        return ModelBase
    except (ImportError, AttributeError):
        return None


__all__ = ["ChatResponse", "make_gateway_model", "set_client"]
