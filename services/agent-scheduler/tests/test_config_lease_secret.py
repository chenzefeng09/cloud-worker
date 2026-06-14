"""Unit tests for the fail-closed broker-lease-secret startup guard.

The scheduler signs the per-run capability leases that tool-broker validates.
A placeholder/weak ``tool_broker_lease_secret`` would let a sandbox forge its
own leases and defeat every scope/tenant boundary, so outside dev-like
environments the scheduler refuses to start in broker egress mode with a weak
secret.  The guard is *scoped*: when egress does not run through the broker the
secret is unused and must not block startup.
"""

from __future__ import annotations

import secrets

import pytest

from agent_scheduler.config import SANDBOX_TOKEN_SCOPES, Settings

_PLACEHOLDER = "dev-tool-broker-lease-secret-change-me"


def test_broker_mode_prod_rejects_placeholder_secret() -> None:
    with pytest.raises(ValueError, match="SCHEDULER_TOOL_BROKER_LEASE_SECRET"):
        Settings(
            env="prod",
            sandbox_egress_mode="broker",
            tool_broker_lease_secret=_PLACEHOLDER,
        )


def test_broker_mode_prod_rejects_short_secret() -> None:
    with pytest.raises(ValueError, match="SCHEDULER_TOOL_BROKER_LEASE_SECRET"):
        Settings(
            env="staging",
            sandbox_egress_mode="broker",
            tool_broker_lease_secret="too-short",
        )


def test_broker_mode_prod_accepts_strong_secret() -> None:
    strong = secrets.token_urlsafe(48)
    assert len(strong) >= 32
    settings = Settings(
        env="prod",
        sandbox_egress_mode="broker",
        tool_broker_lease_secret=strong,
    )
    assert settings.tool_broker_lease_secret == strong


def test_non_broker_mode_prod_allows_placeholder_secret() -> None:
    # Secret is unused outside broker egress mode, so it must not block startup.
    settings = Settings(
        env="prod",
        sandbox_egress_mode="foundation",
        tool_broker_lease_secret=_PLACEHOLDER,
    )
    assert settings.sandbox_egress_mode == "foundation"


def test_dev_broker_mode_allows_placeholder_secret() -> None:
    # Dev (and test/local) tolerate the shipped placeholder so the local stack
    # just works without per-developer secret minting.
    settings = Settings(
        env="dev",
        sandbox_egress_mode="broker",
        tool_broker_lease_secret=_PLACEHOLDER,
    )
    assert settings.env == "dev"


# --- Security fix B7: least-privilege sandbox token scopes ------------------


def test_sandbox_token_scopes_excludes_broker_unreachable_caps() -> None:
    """``notification.send`` was a dead capability and must stay removed.

    Nothing in the sandbox/worker path consumes it, there is no tool-broker
    route proxying to a notification service, and it is absent from
    ``egress_policy.BROKER_LEASE_SCOPES`` — carrying it only widened the
    token's blast radius if leaked.
    """
    assert "notification.send" not in SANDBOX_TOKEN_SCOPES


def test_sandbox_token_scopes_keeps_required_caps() -> None:
    """The capabilities the sandbox genuinely uses must remain.

    * ``llm-gateway.invoke`` — gates the model-call façade (broker + direct).
    * ``audit.write`` — the ``event_audit_sink`` worker extension POSTs agent
      events directly to the foundation audit service in ``foundation`` egress
      mode, where the scope is genuinely enforced; dropping it would break
      audit forwarding there.
    """
    assert "llm-gateway.invoke" in SANDBOX_TOKEN_SCOPES
    assert "audit.write" in SANDBOX_TOKEN_SCOPES
