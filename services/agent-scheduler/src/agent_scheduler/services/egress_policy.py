"""Sandbox egress policy helpers.

The scheduler owns the trust boundary between untrusted agent code and
foundation services.  Keep the policy translation here so Docker and Cube
runtimes do not grow separate, inconsistent URL/network rules.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any


BROKER_LEASE_SCOPES: tuple[str, ...] = (
    "llm-gateway.invoke",
    "mcp.read_internal",
    # Tenant MCP server tool invocation via the tool-broker /mcp-tenant
    # passthrough route; without it the broker returns 403 on that route.
    "mcp.tenant_invoke",
    "skill.read_internal",
    "web_fetch.invoke",
    # DLP tool-call scanning is mediated by the tool-broker /dlp façade so the
    # worker can submit content to foundation-dlp's /core/scan endpoint without
    # direct infra network reach.
    "dlp.scan",
    # State (Redis hot-state) and object-store reads are mediated by the
    # tool-broker /state and /s3 façades so a sandbox with no direct infra
    # network can still checkpoint run state and read tenant-scoped objects.
    "state.write",
    "s3.read",
)


def _trim(value: str) -> str:
    return value.rstrip("/")


def _broker_url(settings: Any, suffix: str) -> str:
    base = _trim(str(getattr(settings, "tool_broker_url", "") or ""))
    if not base:
        return ""
    return f"{base}/{suffix.lstrip('/')}"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def mint_tool_broker_lease(
    settings: Any,
    *,
    tenant_id: str,
    user_id: str,
    run_id: str,
    ttl_seconds: int,
) -> str:
    """Return a signed per-run lease for tool-broker, or ``""`` outside broker mode.

    The lease is deliberately smaller than a platform token: it only proves this
    sandbox may use a fixed set of broker façades for this tenant/run until
    expiry.  The foundation bearer token is still forwarded to upstream services.
    """
    mode = str(getattr(settings, "sandbox_egress_mode", "foundation") or "foundation")
    if mode != "broker":
        return ""

    secret = str(getattr(settings, "tool_broker_lease_secret", "") or "")
    if not secret:
        raise ValueError("tool_broker_lease_secret is required in broker egress mode")

    now = int(time.time())
    ttl = min(max(int(ttl_seconds or 0), 60), 86_400)
    payload = {
        "v": 1,
        "tenant_id": tenant_id,
        "user_id": user_id,
        "run_id": run_id,
        "scopes": list(BROKER_LEASE_SCOPES),
        "iat": now,
        "exp": now + ttl,
    }
    body = _b64url(
        json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    )
    sig = _b64url(
        hmac.new(
            secret.encode("utf-8"),
            body.encode("ascii"),
            hashlib.sha256,
        ).digest()
    )
    return f"v1.{body}.{sig}"


def service_env(settings: Any, *, llm_gateway_url: str | None = None) -> dict[str, str]:
    """Return service URL + infra-credential env vars for the egress mode.

    This is the single source of truth for every standing infra credential a
    sandbox might receive (service URLs, the password-bearing Redis URL, and
    the S3 endpoint/bucket/region/keys).  Runtimes MUST read these values from
    here rather than from ``settings`` directly, so the mode logic cannot be
    bypassed for one credential while being honoured for another.

    Modes:
    - ``foundation`` keeps legacy direct service URLs and real infra creds.
    - ``broker`` exposes only tool-broker façade URLs, blanks the direct
      Redis URL and the S3 access/secret keys, but keeps the S3 bucket/region
      (names, not secrets — the worker needs the bucket NAME to build keys for
      the broker ``/s3/v1/object?bucket=`` call, and the region is harmless).
    - ``none`` blanks all network service URLs and infra creds so only local
      filesystem tools work.
    """
    mode = str(getattr(settings, "sandbox_egress_mode", "foundation") or "foundation")
    if mode == "foundation":
        return {
            "SANDBOX_EGRESS_MODE": mode,
            "TOOL_BROKER_URL": "",
            "LLM_GATEWAY_URL": llm_gateway_url or settings.llm_gateway_url,
            "MCP_REGISTRY_URL": settings.mcp_registry_url,
            "SKILL_STORE_URL": settings.skill_store_url,
            "WEB_FETCH_GATEWAY_URL": settings.web_fetch_gateway_url,
            "DLP_URL": settings.dlp_url,
            "S3_ENDPOINT_URL": settings.s3_endpoint_url,
            "S3_BUCKET": settings.s3_bucket,
            "S3_REGION": settings.s3_region,
            "S3_ACCESS_KEY_ID": settings.s3_access_key_id,
            "S3_SECRET_ACCESS_KEY": settings.s3_secret_access_key,
            "REDIS_URL": settings.redis_url,
        }

    if mode == "broker":
        return {
            "SANDBOX_EGRESS_MODE": mode,
            "TOOL_BROKER_URL": _trim(settings.tool_broker_url),
            # OpenAI-compatible base; llm_gateway_enforcer appends
            # /chat/completions to this value.
            "LLM_GATEWAY_URL": _broker_url(settings, "llm-gateway/v1"),
            # mcp_tenant_injector appends /internal/mcp-servers.
            "MCP_REGISTRY_URL": _broker_url(settings, "mcp/v1"),
            # Kept for old skills_loader deployments that still resolve in
            # sandbox; current scheduler-side mounted skills do not need it.
            "SKILL_STORE_URL": _broker_url(settings, "skills/v1"),
            # WebFetchTool appends /fetch when the URL ends with /api/web-fetch/v1.
            "WEB_FETCH_GATEWAY_URL": _broker_url(
                settings, "web-fetch/api/web-fetch/v1"
            ),
            # The worker appends /core/scan; the broker /dlp route allowlists
            # that single path on the foundation-dlp upstream.
            "DLP_URL": _broker_url(settings, "dlp/api/dlp/v1"),
            # Direct object-store endpoint and Redis access must not be
            # available in broker mode; the worker reaches both through the
            # tool-broker /s3 and /state façades, which hold their own creds.
            "S3_ENDPOINT_URL": "",
            # Bucket NAME and region are not secrets — the worker still needs
            # the bucket name to build keys for the broker /s3/v1/object call.
            "S3_BUCKET": settings.s3_bucket,
            "S3_REGION": settings.s3_region,
            # The MinIO root access/secret keys are the actual credential and
            # MUST be blanked: the broker holds the real keys, the worker
            # sends none.  Future broker endpoints must be added through
            # TOOL_BROKER_URL, never by restoring these.
            "S3_ACCESS_KEY_ID": "",
            "S3_SECRET_ACCESS_KEY": "",
            "REDIS_URL": "",
        }

    if mode == "none":
        return {
            "SANDBOX_EGRESS_MODE": mode,
            "TOOL_BROKER_URL": "",
            "LLM_GATEWAY_URL": "",
            "MCP_REGISTRY_URL": "",
            "SKILL_STORE_URL": "",
            "WEB_FETCH_GATEWAY_URL": "",
            "DLP_URL": "",
            "S3_ENDPOINT_URL": "",
            "S3_BUCKET": "",
            "S3_REGION": "",
            "S3_ACCESS_KEY_ID": "",
            "S3_SECRET_ACCESS_KEY": "",
            "REDIS_URL": "",
        }

    raise ValueError(f"unsupported sandbox_egress_mode: {mode!r}")


def docker_run_network_kwargs(settings: Any) -> dict[str, Any]:
    """Return Docker SDK kwargs that enforce the selected egress mode."""
    mode = str(getattr(settings, "sandbox_egress_mode", "foundation") or "foundation")
    if mode == "foundation":
        return {"network": settings.docker_network}
    if mode == "broker":
        return {"network": settings.sandbox_broker_network}
    if mode == "none":
        return {"network_disabled": True}
    raise ValueError(f"unsupported sandbox_egress_mode: {mode!r}")


def metadata(settings: Any) -> dict[str, str]:
    """Labels/metadata describing the egress policy applied to a sandbox."""
    mode = str(getattr(settings, "sandbox_egress_mode", "foundation") or "foundation")
    out = {"agent-platform.egress-mode": mode}
    if mode == "broker":
        out["agent-platform.tool-broker-url"] = _trim(settings.tool_broker_url)
    return out
