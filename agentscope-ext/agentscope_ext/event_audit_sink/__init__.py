"""Forward agent ``post_print`` events to ``foundation.audit``.

Layered on top of the default ``attach_hooks``: the disk log
(``events.jsonl``) is still written by the runtime worker; this
extension adds a second hook that POSTs each event to the foundation
audit service so a hard sandbox crash cannot lose the trail.

Activation: call :func:`install` exactly once (it is idempotent — a
re-import or a second ``RUNTIME_EXTENSIONS`` pass is harmless).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Mapping

from agentscope_ext.runtime_worker import hooks as _hooks

from . import sink

_LOG = logging.getLogger(__name__)

# Module-level sentinel so a second install() call is a no-op rather
# than a second wrapper around the same attach_hooks.
_INSTALLED = False


def install() -> None:
    """Wrap ``hooks.attach_hooks`` with the audit-POST sink.

    The wrap pattern preserves whatever ``attach_hooks`` was set before
    (default disk sink, or another extension's wrapper).
    """
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    previous = _hooks.attach_hooks

    def _attach_with_audit(
        agent: Any, *, events_path: Path, env: Mapping[str, str]
    ) -> None:
        previous(agent, events_path=events_path, env=env)
        _attach_audit_hook(agent, env=env)

    _hooks.attach_hooks = _attach_with_audit


def _attach_audit_hook(agent: Any, *, env: Mapping[str, str]) -> None:
    """Register the second ``post_print`` hook for audit forwarding."""
    tenant_id = env.get("FOUNDATION_TENANT_ID", "")
    user_id = env.get("FOUNDATION_USER_ID", "")
    base_url = env.get("FOUNDATION_API_URL", "")
    token = env.get("FOUNDATION_API_TOKEN", "")

    def _post_print_hook(self: Any, kwargs: dict[str, Any]) -> None:
        msg = kwargs.get("msg")
        if msg is None:
            return
        try:
            if hasattr(msg, "to_dict"):
                msg_payload = msg.to_dict()
            else:
                msg_payload = {"content": str(msg)}
        except Exception as exc:  # pragma: no cover -- defensive
            msg_payload = {"error": f"serialise failed: {exc!r}"}
        body = {
            "tenant_id": tenant_id,
            "user_id": user_id,
            "event_type": "agent_msg",
            "resource": "agent_session",
            "payload": msg_payload,
        }
        try:
            sink.audit_post(base_url=base_url, token=token, body=body)
        except Exception as exc:  # noqa: BLE001 -- never break the agent
            _LOG.warning("event_audit_sink: POST failed: %r", exc)

    try:
        agent.register_instance_hook(
            hook_type="post_print",
            hook_name="event_audit_sink_post",
            hook=_post_print_hook,
        )
    except Exception as exc:  # noqa: BLE001 -- defensive
        _LOG.warning("event_audit_sink: failed to register hook: %r", exc)


__all__ = ["install"]
