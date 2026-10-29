"""Unit tests for ``egress_policy.service_env`` — the single source of truth
for every standing infra credential a sandbox might receive.

The defense-in-depth invariant these tests pin: in broker (and none) mode the
MinIO root access/secret keys and the password-bearing Redis URL must NOT leak
into the sandbox contract, while in foundation mode the legacy direct creds are
still present.
"""

from __future__ import annotations

from agent_scheduler.config import Settings
from agent_scheduler.services import egress_policy


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "env": "test",
        "log_level": "WARNING",
        "agent_image": "hello-world:latest",
        "workspace_base": "/tmp/agent-platform-test",
        "docker_network": "none",
        "foundation_identity_url": "http://identity-test/api/identity/v1",
        "scheduler_service_token": "fake-service-token",
        "otel_enabled": False,
        "s3_endpoint_url": "http://foundation-minio:9000",
        "s3_bucket": "agent-platform",
        "s3_region": "us-east-1",
        "s3_access_key_id": "minio-root",
        "s3_secret_access_key": "minio-root-secret",
        "redis_url": "redis://:secret@foundation-redis:6379/0",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def test_foundation_mode_returns_real_s3_creds_and_redis() -> None:
    env = egress_policy.service_env(_settings(sandbox_egress_mode="foundation"))
    assert env["S3_ENDPOINT_URL"] == "http://foundation-minio:9000"
    assert env["S3_BUCKET"] == "agent-platform"
    assert env["S3_REGION"] == "us-east-1"
    assert env["S3_ACCESS_KEY_ID"] == "minio-root"
    assert env["S3_SECRET_ACCESS_KEY"] == "minio-root-secret"
    assert env["REDIS_URL"] == "redis://:secret@foundation-redis:6379/0"


def test_broker_mode_blanks_s3_access_keys_and_redis() -> None:
    env = egress_policy.service_env(
        _settings(
            sandbox_egress_mode="broker",
            tool_broker_url="http://tool-broker:8024/api/tool-broker/v1",
        )
    )
    # The actual credentials must be gone.
    assert env["S3_ACCESS_KEY_ID"] == ""
    assert env["S3_SECRET_ACCESS_KEY"] == ""
    assert env["S3_ENDPOINT_URL"] == ""
    assert env["REDIS_URL"] == ""
    # Bucket NAME + region are not secrets — the worker still needs the bucket
    # name to build keys for the broker /s3/v1/object call.
    assert env["S3_BUCKET"] == "agent-platform"
    assert env["S3_REGION"] == "us-east-1"


def test_broker_mode_routes_mcp_and_skill_through_broker_facade() -> None:
    """Broker mode MUST point MCP/skill URLs at the tool-broker façade.

    In broker egress mode the sandbox's ``MCP_REGISTRY_URL`` /
    ``SKILL_STORE_URL`` must resolve to the tool-broker, not the direct
    mcp-registry / skill-store service.  Forcing traffic through the broker is
    what lets the broker strip sandbox-supplied ``X-Actor`` (an unconditional
    RBAC bypass in those services' internal guards).  If these URLs silently
    regressed to the direct service, the broker's strip protection would be
    bypassed and the X-Actor:ADMIN primitive would become reachable again.
    """
    settings = _settings(
        sandbox_egress_mode="broker",
        tool_broker_url="http://tool-broker:8024/api/tool-broker/v1",
    )
    env = egress_policy.service_env(settings)

    # Traffic is forced through the broker façade...
    assert "tool-broker" in env["MCP_REGISTRY_URL"]
    assert "tool-broker" in env["SKILL_STORE_URL"]
    assert env["MCP_REGISTRY_URL"].startswith(
        "http://tool-broker:8024/api/tool-broker/v1"
    )
    assert env["SKILL_STORE_URL"].startswith(
        "http://tool-broker:8024/api/tool-broker/v1"
    )
    # ...and is NOT the direct service URL (where X-Actor would not be stripped).
    assert env["MCP_REGISTRY_URL"] != settings.mcp_registry_url
    assert env["SKILL_STORE_URL"] != settings.skill_store_url


def test_foundation_mode_exposes_direct_mcp_and_skill_urls() -> None:
    """Foundation mode is the KNOWN residual-risk path — asserted, not silent.

    In foundation mode ``MCP_REGISTRY_URL`` / ``SKILL_STORE_URL`` are the DIRECT
    service URLs (no broker hop), so the tool-broker's ``X-Actor`` strip
    protection does NOT apply: a sandbox reaches mcp-registry / skill-store
    directly.  This test documents that exposure as a visible, asserted property
    rather than a silent surprise; broker mode (tested above) is the protected
    path.
    """
    settings = _settings(sandbox_egress_mode="foundation")
    env = egress_policy.service_env(settings)

    assert env["MCP_REGISTRY_URL"] == settings.mcp_registry_url
    assert env["SKILL_STORE_URL"] == settings.skill_store_url


def test_dlp_url_present_in_all_three_modes() -> None:
    """DLP_URL must be distributed in every egress mode.

    Foundation mode exposes the direct foundation-dlp URL; broker mode rewrites
    it to the tool-broker /dlp façade (whose /core/scan path is allowlisted);
    none mode blanks it.  The worker POSTs to ``${DLP_URL}/core/scan``, so the
    base must omit that suffix.
    """
    foundation = egress_policy.service_env(
        _settings(
            sandbox_egress_mode="foundation",
            dlp_url="http://foundation-dlp:8013/api/dlp/v1",
        )
    )
    assert foundation["DLP_URL"] == "http://foundation-dlp:8013/api/dlp/v1"

    broker = egress_policy.service_env(
        _settings(
            sandbox_egress_mode="broker",
            tool_broker_url="http://tool-broker:8024/api/tool-broker/v1",
        )
    )
    # Forced through the broker façade (not the direct DLP service), and the
    # base ends at the route prefix so the worker can append /core/scan.
    assert broker["DLP_URL"] == (
        "http://tool-broker:8024/api/tool-broker/v1/dlp/api/dlp/v1"
    )
    assert "tool-broker" in broker["DLP_URL"]

    none = egress_policy.service_env(_settings(sandbox_egress_mode="none"))
    assert none["DLP_URL"] == ""


def test_dlp_scan_is_a_granted_broker_lease_scope() -> None:
    """The per-run broker lease must grant ``dlp.scan`` so the /dlp façade is
    reachable; the tool-broker route checks for exactly this scope."""
    assert "dlp.scan" in egress_policy.BROKER_LEASE_SCOPES


def test_mcp_tenant_invoke_is_a_granted_broker_lease_scope() -> None:
    """The per-run broker lease must grant ``mcp.tenant_invoke`` so the
    /mcp-tenant/v1/{server_id} passthrough route is reachable; the tool-broker
    route checks for exactly this scope and returns 403 without it."""
    assert "mcp.tenant_invoke" in egress_policy.BROKER_LEASE_SCOPES


def test_knowledge_search_is_a_granted_broker_lease_scope() -> None:
    """The per-run broker lease must grant ``knowledge.search`` so the
    /knowledge/v1/search façade is reachable; the tool-broker route checks for
    exactly this scope and returns 403 without it."""
    assert "knowledge.search" in egress_policy.BROKER_LEASE_SCOPES


def test_knowledge_ingest_url_present_in_all_three_modes() -> None:
    """KNOWLEDGE_INGEST_URL must be distributed in every egress mode.

    Foundation mode exposes the direct ki URL; broker mode rewrites it to the
    tool-broker /knowledge/v1 façade (whose /search path is allowlisted); none
    mode blanks it so the knowledge_tool self-gates off.  The worker GETs
    ``{KNOWLEDGE_INGEST_URL}/search``, so the base must omit that suffix.
    """
    foundation = egress_policy.service_env(
        _settings(
            sandbox_egress_mode="foundation",
            knowledge_ingest_url="http://knowledge-ingest:8012/api/knowledge/v1",
        )
    )
    assert foundation["KNOWLEDGE_INGEST_URL"] == (
        "http://knowledge-ingest:8012/api/knowledge/v1"
    )

    broker = egress_policy.service_env(
        _settings(
            sandbox_egress_mode="broker",
            tool_broker_url="http://tool-broker:8024/api/tool-broker/v1",
            knowledge_ingest_url="http://knowledge-ingest:8012/api/knowledge/v1",
        )
    )
    # Forced through the broker façade (not the direct ki service), base ends at
    # the route prefix so the worker can append /search.
    assert broker["KNOWLEDGE_INGEST_URL"] == (
        "http://tool-broker:8024/api/tool-broker/v1/knowledge/v1"
    )
    assert "tool-broker" in broker["KNOWLEDGE_INGEST_URL"]

    none = egress_policy.service_env(_settings(sandbox_egress_mode="none"))
    assert none["KNOWLEDGE_INGEST_URL"] == ""


def test_knowledge_ingest_url_key_shape_consistent_across_modes() -> None:
    """KNOWLEDGE_INGEST_URL must appear in every mode's service_env dict so a
    runtime reading ``service_env[...]`` never KeyErrors regardless of mode."""
    foundation = set(egress_policy.service_env(_settings(sandbox_egress_mode="foundation")))
    broker = set(
        egress_policy.service_env(
            _settings(
                sandbox_egress_mode="broker",
                tool_broker_url="http://tool-broker:8024/api/tool-broker/v1",
            )
        )
    )
    none = set(egress_policy.service_env(_settings(sandbox_egress_mode="none")))
    assert "KNOWLEDGE_INGEST_URL" in foundation
    assert "KNOWLEDGE_INGEST_URL" in broker
    assert "KNOWLEDGE_INGEST_URL" in none


def test_mint_lease_embeds_clearance_in_broker_mode() -> None:
    """The signed broker lease carries the run's effective clearance so the
    broker can re-inject X-User-Clearance for the ki façade from a trusted
    source.  The clearance is NOT secret -> it rides in the signed payload."""
    import base64
    import json

    settings = _settings(
        sandbox_egress_mode="broker",
        tool_broker_url="http://tool-broker:8024/api/tool-broker/v1",
        tool_broker_lease_secret="a" * 40,
    )
    token = egress_policy.mint_tool_broker_lease(
        settings,
        tenant_id="tnt_x",
        user_id="usr_x",
        run_id="run_x",
        ttl_seconds=600,
        clearance="L3",
    )
    assert token.startswith("v1.")
    _, body, _ = token.split(".", 2)
    padding = "=" * (-len(body) % 4)
    payload = json.loads(base64.urlsafe_b64decode((body + padding).encode("ascii")))
    assert payload["clearance"] == "L3"
    assert "knowledge.search" in payload["scopes"]


def test_mint_lease_omits_clearance_when_empty() -> None:
    """An empty clearance is omitted from the lease payload (graceful: older
    callers / non-ki runs), and the broker simply does not re-inject it."""
    import base64
    import json

    settings = _settings(
        sandbox_egress_mode="broker",
        tool_broker_url="http://tool-broker:8024/api/tool-broker/v1",
        tool_broker_lease_secret="a" * 40,
    )
    token = egress_policy.mint_tool_broker_lease(
        settings,
        tenant_id="tnt_x",
        user_id="usr_x",
        run_id="run_x",
        ttl_seconds=600,
        clearance="",
    )
    _, body, _ = token.split(".", 2)
    padding = "=" * (-len(body) % 4)
    payload = json.loads(base64.urlsafe_b64decode((body + padding).encode("ascii")))
    assert "clearance" not in payload


def test_mint_lease_returns_empty_outside_broker_mode() -> None:
    """Foundation / none modes do not mint a lease (no broker to validate it)."""
    settings = _settings(sandbox_egress_mode="foundation")
    token = egress_policy.mint_tool_broker_lease(
        settings,
        tenant_id="tnt_x",
        user_id="usr_x",
        run_id="run_x",
        ttl_seconds=600,
        clearance="L3",
    )
    assert token == ""


def test_none_mode_blanks_every_s3_field_and_redis() -> None:
    env = egress_policy.service_env(_settings(sandbox_egress_mode="none"))
    for key in (
        "S3_ENDPOINT_URL",
        "S3_BUCKET",
        "S3_REGION",
        "S3_ACCESS_KEY_ID",
        "S3_SECRET_ACCESS_KEY",
        "REDIS_URL",
    ):
        assert env[key] == "", f"{key} must be blank in none mode"


def test_all_modes_share_the_same_s3_redis_key_shape() -> None:
    """Dict shape must be identical across modes — only values differ — so a
    runtime reading ``service_env[...]`` never KeyErrors regardless of mode."""
    infra_keys = {
        "S3_ENDPOINT_URL",
        "S3_BUCKET",
        "S3_REGION",
        "S3_ACCESS_KEY_ID",
        "S3_SECRET_ACCESS_KEY",
        "REDIS_URL",
    }
    foundation = set(egress_policy.service_env(_settings(sandbox_egress_mode="foundation")))
    broker = set(
        egress_policy.service_env(
            _settings(
                sandbox_egress_mode="broker",
                tool_broker_url="http://tool-broker:8024/api/tool-broker/v1",
            )
        )
    )
    none = set(egress_policy.service_env(_settings(sandbox_egress_mode="none")))
    assert infra_keys <= foundation
    assert infra_keys <= broker
    assert infra_keys <= none
