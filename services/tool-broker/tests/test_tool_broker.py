from __future__ import annotations

import base64
import hashlib
import hmac
import json
import sys
import time
from pathlib import Path
from typing import Any

import httpx
from fastapi.testclient import TestClient

_PKG_ROOT = Path(__file__).resolve().parent.parent
_SRC = _PKG_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from tool_broker.config import Settings
from tool_broker.main import create_app

_LEASE_SECRET = "test-tool-broker-secret"


class _FakeAsyncClient:
    calls: list[dict[str, Any]] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.kwargs = kwargs

    async def __aenter__(self) -> "_FakeAsyncClient":
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        self.calls.append({"method": method, "url": url, **kwargs})
        return httpx.Response(
            200,
            json={"ok": True, "target": url},
            headers={"content-type": "application/json", "connection": "close"},
            request=httpx.Request(method, url),
        )


def _client(require_auth: bool = True) -> TestClient:
    settings = Settings(
        require_auth=require_auth,
        require_lease=True,
        lease_secret=_LEASE_SECRET,
        llm_gateway_url="http://llm-gateway:8011/api/llm-gateway/v1",
        mcp_registry_url="http://mcp-registry:8010/api/mcp/v1",
        skill_store_url="http://skill-store:8011/api/skills/v1",
        web_fetch_gateway_url="http://web-fetch-gateway:8013/api/web-fetch/v1",
    )
    return TestClient(create_app(settings))


def _lease(
    scopes: list[str],
    *,
    tenant_id: str = "tnt_123",
    user_id: str = "usr_123",
    run_id: str = "run_123",
) -> str:
    payload = {
        "v": 1,
        "tenant_id": tenant_id,
        "user_id": user_id,
        "run_id": run_id,
        "scopes": scopes,
        "iat": int(time.time()),
        "exp": int(time.time()) + 600,
    }
    body = base64.urlsafe_b64encode(
        json.dumps(
            payload,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).rstrip(b"=").decode("ascii")
    sig = base64.urlsafe_b64encode(
        hmac.new(
            _LEASE_SECRET.encode("utf-8"),
            body.encode("ascii"),
            hashlib.sha256,
        ).digest()
    ).rstrip(b"=").decode("ascii")
    return f"v1.{body}.{sig}"


def test_auth_required() -> None:
    client = _client(require_auth=True)

    resp = client.post(
        "/api/tool-broker/v1/llm-gateway/v1/chat/completions",
        json={"messages": []},
    )

    assert resp.status_code == 401
    assert "Authorization" in resp.text


def test_llm_gateway_proxy_forwards_to_whitelisted_upstream(monkeypatch) -> None:
    _FakeAsyncClient.calls = []
    monkeypatch.setattr("tool_broker.main.httpx.AsyncClient", _FakeAsyncClient)
    client = _client(require_auth=True)

    resp = client.post(
        "/api/tool-broker/v1/llm-gateway/v1/chat/completions?trace=1",
        headers={
            "Authorization": "Bearer sandbox-token",
            "X-Tenant-Id": "tnt_123",
            "X-User-Id": "usr_123",
            "X-Tool-Broker-Lease": _lease(["llm-gateway.invoke"]),
            "Host": "attacker.example",
            "X-Forwarded-For": "10.0.0.1",
        },
        json={"model": "m", "messages": []},
    )

    assert resp.status_code == 200
    assert resp.json()["target"] == (
        "http://llm-gateway:8011/api/llm-gateway/v1/chat/completions?trace=1"
    )
    assert len(_FakeAsyncClient.calls) == 1
    call = _FakeAsyncClient.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == (
        "http://llm-gateway:8011/api/llm-gateway/v1/chat/completions?trace=1"
    )
    forwarded = {k.lower(): v for k, v in call["headers"].items()}
    assert forwarded["authorization"] == "Bearer sandbox-token"
    assert forwarded["x-tenant-id"] == "tnt_123"
    assert "host" not in forwarded
    assert "x-forwarded-for" not in forwarded
    assert "x-tool-broker-lease" not in forwarded
    assert "connection" not in resp.headers


def test_wrong_lease_scope_is_rejected(monkeypatch) -> None:
    _FakeAsyncClient.calls = []
    monkeypatch.setattr("tool_broker.main.httpx.AsyncClient", _FakeAsyncClient)
    client = _client(require_auth=True)

    resp = client.post(
        "/api/tool-broker/v1/llm-gateway/v1/chat/completions",
        headers={
            "Authorization": "Bearer sandbox-token",
            "X-Tenant-Id": "tnt_123",
            "X-User-Id": "usr_123",
            "X-Tool-Broker-Lease": _lease(["web_fetch.invoke"]),
        },
        json={"model": "m", "messages": []},
    )

    assert resp.status_code == 403
    assert _FakeAsyncClient.calls == []


def test_web_fetch_proxy_path_matches_worker_contract(monkeypatch) -> None:
    _FakeAsyncClient.calls = []
    monkeypatch.setattr("tool_broker.main.httpx.AsyncClient", _FakeAsyncClient)
    client = _client(require_auth=True)

    resp = client.post(
        "/api/tool-broker/v1/web-fetch/api/web-fetch/v1/fetch",
        headers={
            "Authorization": "Bearer sandbox-token",
            "X-Tenant-Id": "tnt_123",
            "X-User-Id": "usr_123",
            "X-Service-Scopes": "web_fetch.invoke",
            "X-Tool-Broker-Lease": _lease(["web_fetch.invoke"]),
        },
        json={"url": "https://example.com"},
    )

    assert resp.status_code == 200
    assert _FakeAsyncClient.calls[0]["url"] == (
        "http://web-fetch-gateway:8013/api/web-fetch/v1/fetch"
    )


# ---------------------------------------------------------------------------
# /state façade (Redis hot-state)
# ---------------------------------------------------------------------------


class _FakeRedis:
    """Async Redis stub recording the broker's translated commands."""

    def __init__(self) -> None:
        self.set_calls: list[tuple[str, str, dict[str, Any]]] = []
        self.get_calls: list[str] = []
        self.xadd_calls: list[tuple[str, dict[str, str], dict[str, Any]]] = []
        self.expire_calls: list[tuple[str, int]] = []
        self.store: dict[str, str] = {}

    async def set(self, key: str, value: str, **kwargs: Any) -> None:
        self.set_calls.append((key, value, kwargs))
        self.store[key] = value

    async def get(self, key: str) -> str | None:
        self.get_calls.append(key)
        return self.store.get(key)

    async def xadd(self, key: str, fields: dict[str, str], **kwargs: Any) -> str:
        self.xadd_calls.append((key, fields, kwargs))
        return "1-0"

    async def expire(self, key: str, ttl: int) -> None:
        self.expire_calls.append((key, ttl))


def _state_client(fake_redis: _FakeRedis, require_auth: bool = True) -> TestClient:
    settings = Settings(
        require_auth=require_auth,
        require_lease=True,
        lease_secret=_LEASE_SECRET,
        llm_gateway_url="http://llm-gateway:8011/api/llm-gateway/v1",
        mcp_registry_url="http://mcp-registry:8010/api/mcp/v1",
        skill_store_url="http://skill-store:8011/api/skills/v1",
        web_fetch_gateway_url="http://web-fetch-gateway:8013/api/web-fetch/v1",
    )
    app = create_app(settings)
    app.state.redis_client = fake_redis
    return TestClient(app)


def _state_headers(scopes: list[str], *, run_id: str = "run_123") -> dict[str, str]:
    return {
        "Authorization": "Bearer sandbox-token",
        "X-Tenant-Id": "tnt_123",
        "X-User-Id": "usr_123",
        "X-Tool-Broker-Lease": _lease(scopes, run_id=run_id),
    }


def test_state_set_maps_to_redis_set_with_ttl() -> None:
    redis = _FakeRedis()
    client = _state_client(redis)

    resp = client.post(
        "/api/tool-broker/v1/state/v1/set",
        headers=_state_headers(["state.write"]),
        json={"run_id": "run_123", "kind": "state", "value": "{}", "ttl": 1800},
    )

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert redis.set_calls == [("agent:run:run_123:state", "{}", {"ex": 1800})]


def test_state_set_paused_kind_maps_to_paused_key() -> None:
    redis = _FakeRedis()
    client = _state_client(redis)

    resp = client.post(
        "/api/tool-broker/v1/state/v1/set",
        headers=_state_headers(["state.write"]),
        json={"run_id": "run_123", "kind": "paused", "value": "blob", "ttl": 604800},
    )

    assert resp.status_code == 200
    assert redis.set_calls[0][0] == "agent:run:run_123:paused"
    assert redis.set_calls[0][2] == {"ex": 604800}


def test_state_get_returns_value() -> None:
    redis = _FakeRedis()
    redis.store["agent:run:run_123:state"] = '{"step": 3}'
    client = _state_client(redis)

    resp = client.get(
        "/api/tool-broker/v1/state/v1/get?run_id=run_123&kind=state",
        headers=_state_headers(["state.write"]),
    )

    assert resp.status_code == 200
    assert resp.json() == {"value": '{"step": 3}'}
    assert redis.get_calls == ["agent:run:run_123:state"]


def test_state_get_returns_null_when_absent() -> None:
    redis = _FakeRedis()
    client = _state_client(redis)

    resp = client.get(
        "/api/tool-broker/v1/state/v1/get?run_id=run_123&kind=state",
        headers=_state_headers(["state.write"]),
    )

    assert resp.status_code == 200
    assert resp.json() == {"value": None}


def test_state_xadd_maps_to_redis_xadd_and_expire() -> None:
    redis = _FakeRedis()
    client = _state_client(redis)

    resp = client.post(
        "/api/tool-broker/v1/state/v1/xadd",
        headers=_state_headers(["state.write"]),
        json={
            "run_id": "run_123",
            "fields": {"type": "delta", "text": "hi"},
            "maxlen": 10000,
            "expire": 1800,
        },
    )

    assert resp.status_code == 200
    assert redis.xadd_calls == [
        (
            "agent:run:run_123:tokens",
            {"type": "delta", "text": "hi"},
            {"maxlen": 10000, "approximate": True},
        )
    ]
    assert redis.expire_calls == [("agent:run:run_123:tokens", 1800)]


def test_state_xadd_without_expire_does_not_call_expire() -> None:
    redis = _FakeRedis()
    client = _state_client(redis)

    resp = client.post(
        "/api/tool-broker/v1/state/v1/xadd",
        headers=_state_headers(["state.write"]),
        json={"run_id": "run_123", "fields": {"type": "end", "text": ""}},
    )

    assert resp.status_code == 200
    assert redis.expire_calls == []


def test_state_run_id_mismatch_is_rejected() -> None:
    redis = _FakeRedis()
    client = _state_client(redis)

    # Lease is for run_123, but the body asks for run_999 → 403, no Redis op.
    resp = client.post(
        "/api/tool-broker/v1/state/v1/set",
        headers=_state_headers(["state.write"], run_id="run_123"),
        json={"run_id": "run_999", "kind": "state", "value": "{}", "ttl": 1800},
    )

    assert resp.status_code == 403
    assert redis.set_calls == []


def test_state_wrong_scope_is_rejected() -> None:
    redis = _FakeRedis()
    client = _state_client(redis)

    resp = client.post(
        "/api/tool-broker/v1/state/v1/set",
        headers=_state_headers(["s3.read"]),  # missing state.write
        json={"run_id": "run_123", "kind": "state", "value": "{}", "ttl": 1800},
    )

    assert resp.status_code == 403
    assert redis.set_calls == []


def test_state_unknown_kind_is_rejected() -> None:
    redis = _FakeRedis()
    client = _state_client(redis)

    resp = client.post(
        "/api/tool-broker/v1/state/v1/set",
        headers=_state_headers(["state.write"]),
        json={"run_id": "run_123", "kind": "tokens", "value": "x", "ttl": 60},
    )

    assert resp.status_code == 400
    assert redis.set_calls == []


def test_state_requires_auth() -> None:
    redis = _FakeRedis()
    client = _state_client(redis)

    resp = client.post(
        "/api/tool-broker/v1/state/v1/set",
        json={"run_id": "run_123", "kind": "state", "value": "{}", "ttl": 60},
    )

    assert resp.status_code == 401
    assert redis.set_calls == []


# ---------------------------------------------------------------------------
# /s3 façade (read-only object store)
# ---------------------------------------------------------------------------


class _FakeS3Body:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data


class _FakeS3:
    """boto3 S3 stub: serves canned objects, raises for unknown keys."""

    def __init__(self, objects: dict[tuple[str, str], bytes]) -> None:
        self._objects = objects
        self.calls: list[tuple[str, str]] = []

    def get_object(self, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
        self.calls.append((Bucket, Key))
        try:
            return {"Body": _FakeS3Body(self._objects[(Bucket, Key)])}
        except KeyError as exc:
            raise RuntimeError("NoSuchKey") from exc


def _s3_client_app(fake_s3: _FakeS3) -> TestClient:
    settings = Settings(
        require_auth=True,
        require_lease=True,
        lease_secret=_LEASE_SECRET,
        s3_bucket="agent-platform",
    )
    app = create_app(settings)
    app.state.s3_client = fake_s3
    return TestClient(app)


def test_s3_object_returns_bytes_for_tenant_key() -> None:
    key = "tenant/tnt_123/prompts/identity.md"
    s3 = _FakeS3({("agent-platform", key): b"you are an agent"})
    client = _s3_client_app(s3)

    resp = client.get(
        f"/api/tool-broker/v1/s3/v1/object?bucket=agent-platform&key={key}",
        headers=_state_headers(["s3.read"]),
    )

    assert resp.status_code == 200
    assert resp.content == b"you are an agent"
    assert resp.headers["content-type"] == "application/octet-stream"
    assert s3.calls == [("agent-platform", key)]


def test_s3_object_ignores_request_bucket_uses_settings_bucket() -> None:
    # B8: a sandbox-chosen ?bucket= must be ignored; the broker always reads
    # from its own configured bucket (the broker holds MinIO root creds, so a
    # request-supplied bucket would allow cross-bucket reads of any same-key
    # bucket like backups/billing/logs).
    key = "tenant/tnt_123/prompts/identity.md"
    # Object only exists in the broker's configured bucket. If the handler
    # honoured ?bucket=other-bucket the FakeS3 lookup would miss -> 404.
    s3 = _FakeS3({("agent-platform", key): b"you are an agent"})
    client = _s3_client_app(s3)

    resp = client.get(
        f"/api/tool-broker/v1/s3/v1/object?bucket=other-bucket&key={key}",
        headers=_state_headers(["s3.read"]),
    )

    assert resp.status_code == 200
    assert resp.content == b"you are an agent"
    # get_object was called with the settings bucket, NOT "other-bucket".
    assert s3.calls == [("agent-platform", key)]


def test_s3_object_attachment_key_allowed() -> None:
    key = "tenant/tnt_123/sessions/sess_1/attachments/att_1/report.pdf"
    s3 = _FakeS3({("agent-platform", key): b"%PDF-1.7"})
    client = _s3_client_app(s3)

    resp = client.get(
        f"/api/tool-broker/v1/s3/v1/object?key={key}",
        headers=_state_headers(["s3.read"]),
    )

    assert resp.status_code == 200
    assert resp.content == b"%PDF-1.7"


def test_s3_object_missing_returns_404() -> None:
    s3 = _FakeS3({})
    client = _s3_client_app(s3)

    resp = client.get(
        "/api/tool-broker/v1/s3/v1/object?key=tenant/tnt_123/prompts/soul.md",
        headers=_state_headers(["s3.read"]),
    )

    assert resp.status_code == 404


def test_s3_object_cross_tenant_key_is_rejected() -> None:
    key = "tenant/tnt_OTHER/prompts/identity.md"
    s3 = _FakeS3({("agent-platform", key): b"secret"})
    client = _s3_client_app(s3)

    resp = client.get(
        f"/api/tool-broker/v1/s3/v1/object?key={key}",
        headers=_state_headers(["s3.read"]),
    )

    assert resp.status_code == 403
    assert s3.calls == []


def test_s3_object_non_tenant_key_is_rejected() -> None:
    key = "global/secrets.txt"
    s3 = _FakeS3({("agent-platform", key): b"secret"})
    client = _s3_client_app(s3)

    resp = client.get(
        f"/api/tool-broker/v1/s3/v1/object?key={key}",
        headers=_state_headers(["s3.read"]),
    )

    assert resp.status_code == 403
    assert s3.calls == []


def test_s3_object_wrong_scope_is_rejected() -> None:
    key = "tenant/tnt_123/prompts/identity.md"
    s3 = _FakeS3({("agent-platform", key): b"x"})
    client = _s3_client_app(s3)

    resp = client.get(
        f"/api/tool-broker/v1/s3/v1/object?key={key}",
        headers=_state_headers(["state.write"]),  # missing s3.read
    )

    assert resp.status_code == 403
    assert s3.calls == []


# ---------------------------------------------------------------------------
# P0-1: sandbox-supplied identity/authz headers must never reach upstream
# ---------------------------------------------------------------------------


def test_sandbox_identity_headers_are_dropped_and_lease_wins(monkeypatch) -> None:
    """A sandbox cannot smuggle X-Actor/X-Service-Scopes through the proxy.

    The upstream (mcp-registry) must receive NO ``X-Actor`` — its internal
    guard treats ``X-Actor: ADMIN`` as an unconditional RBAC bypass — and the
    forwarded tenant/user must be re-derived from the validated lease rather
    than echoed straight from the (untrusted) request headers.

    The request-level lease guard already forces the *declared* X-Tenant-Id to
    match the lease tenant, so the sandbox cannot even reach upstream with a
    foreign tenant; what this test pins is that the broker re-injects the lease
    identity and strips the privilege-escalation headers.
    """
    _FakeAsyncClient.calls = []
    monkeypatch.setattr("tool_broker.main.httpx.AsyncClient", _FakeAsyncClient)
    client = _client(require_auth=True)

    resp = client.post(
        "/api/tool-broker/v1/mcp/v1/servers",
        headers={
            "Authorization": "Bearer sandbox-token",
            "X-Tenant-Id": "tnt_123",
            # Sandbox claims it is the platform admin with admin scopes.
            "X-Actor": "ADMIN",
            "X-Service-Scopes": "mcp.admin",
            # ...but supplies no X-User-Id, proving the forwarded one is the
            # lease's user (re-injection), not an echo of the request.
            "X-Tool-Broker-Lease": _lease(
                ["mcp.read_internal"], tenant_id="tnt_123", user_id="usr_123"
            ),
        },
        json={},
    )

    assert resp.status_code == 200
    assert len(_FakeAsyncClient.calls) == 1
    forwarded = {k.lower(): v for k, v in _FakeAsyncClient.calls[0]["headers"].items()}
    # The privilege-escalation header is gone entirely.
    assert "x-actor" not in forwarded
    # The sandbox-supplied service scopes (mcp.admin) are dropped and the
    # broker re-injects ONLY the trusted, route-derived upstream scope so the
    # mcp-registry internal guard still admits the call.
    assert forwarded["x-service-scopes"] == "mcp.read_internal"
    # Identity is re-derived from the lease (the sandbox sent no X-User-Id).
    assert forwarded["x-tenant-id"] == "tnt_123"
    assert forwarded["x-user-id"] == "usr_123"


def test_identity_authz_headers_are_in_strip_denylist() -> None:
    """The four identity/authz headers MUST stay in the hop-by-hop denylist.

    ``_HOP_BY_HOP_REQUEST_HEADERS`` is what the proxy uses to scrub
    sandbox-supplied headers before forwarding upstream.  ``x-actor``,
    ``x-service-scopes``, ``x-user-id`` and ``x-tenant-id`` are the
    privilege-escalation / identity-spoof vectors: if any one is removed from
    this denylist a sandbox could smuggle ``X-Actor: ADMIN`` (an unconditional
    RBAC bypass in the mcp-registry / skill-store internal guards) or spoof the
    upstream tenant/user.

    The behavioral test ``test_sandbox_identity_headers_are_dropped_and_lease_wins``
    only exercises x-actor and x-service-scopes end-to-end (it sends no
    x-user-id), so it would NOT catch x-user-id / x-tenant-id silently falling
    out of the denylist.  This constant-level guard pins all four.
    """
    from tool_broker.main import _HOP_BY_HOP_REQUEST_HEADERS

    for h in ("x-actor", "x-service-scopes", "x-user-id", "x-tenant-id"):
        assert h in _HOP_BY_HOP_REQUEST_HEADERS


def test_mcp_proxy_injects_trusted_service_scope_dropping_spoof(monkeypatch) -> None:
    """A spoofed X-Service-Scopes is replaced by the trusted mcp.read_internal.

    The sandbox sends ``X-Service-Scopes: mcp.admin,evil`` but the broker must
    strip it and inject only ``mcp.read_internal`` — the value mcp-registry's
    ``require_internal_scope`` guard consumes — derived from the route/lease,
    never echoed from the request.
    """
    _FakeAsyncClient.calls = []
    monkeypatch.setattr("tool_broker.main.httpx.AsyncClient", _FakeAsyncClient)
    client = _client(require_auth=True)

    resp = client.post(
        "/api/tool-broker/v1/mcp/v1/servers",
        headers={
            "Authorization": "Bearer sandbox-token",
            "X-Tenant-Id": "tnt_123",
            "X-User-Id": "usr_123",
            "X-Service-Scopes": "mcp.admin,evil",
            "X-Tool-Broker-Lease": _lease(["mcp.read_internal"]),
        },
        json={},
    )

    assert resp.status_code == 200
    forwarded = {k.lower(): v for k, v in _FakeAsyncClient.calls[0]["headers"].items()}
    assert forwarded["x-service-scopes"] == "mcp.read_internal"


def test_skills_proxy_injects_trusted_service_scope_dropping_spoof(monkeypatch) -> None:
    """Skill-store route: spoofed scope dropped, skill.read_internal injected."""
    _FakeAsyncClient.calls = []
    monkeypatch.setattr("tool_broker.main.httpx.AsyncClient", _FakeAsyncClient)
    client = _client(require_auth=True)

    resp = client.get(
        "/api/tool-broker/v1/skills/v1/skills/my-skill",
        headers={
            "Authorization": "Bearer sandbox-token",
            "X-Tenant-Id": "tnt_123",
            "X-User-Id": "usr_123",
            "X-Service-Scopes": "skill.admin,evil",
            "X-Tool-Broker-Lease": _lease(["skill.read_internal"]),
        },
    )

    assert resp.status_code == 200
    forwarded = {k.lower(): v for k, v in _FakeAsyncClient.calls[0]["headers"].items()}
    assert forwarded["x-service-scopes"] == "skill.read_internal"


def test_web_fetch_proxy_injects_trusted_service_scope_dropping_spoof(
    monkeypatch,
) -> None:
    """Web-fetch route: spoofed scope dropped, web_fetch.invoke injected."""
    _FakeAsyncClient.calls = []
    monkeypatch.setattr("tool_broker.main.httpx.AsyncClient", _FakeAsyncClient)
    client = _client(require_auth=True)

    resp = client.post(
        "/api/tool-broker/v1/web-fetch/api/web-fetch/v1/fetch",
        headers={
            "Authorization": "Bearer sandbox-token",
            "X-Tenant-Id": "tnt_123",
            "X-User-Id": "usr_123",
            "X-Service-Scopes": "web_fetch.admin,evil",
            "X-Tool-Broker-Lease": _lease(["web_fetch.invoke"]),
        },
        json={"url": "https://example.com"},
    )

    assert resp.status_code == 200
    forwarded = {k.lower(): v for k, v in _FakeAsyncClient.calls[0]["headers"].items()}
    assert forwarded["x-service-scopes"] == "web_fetch.invoke"


def test_llm_gateway_proxy_forwards_no_service_scopes(monkeypatch) -> None:
    """The llm-gateway route never forwards X-Service-Scopes.

    llm-gateway authenticates via X-LLM-Virtual-Key and does not consume the
    scope header; a sandbox-supplied value must be stripped and nothing
    re-injected.
    """
    _FakeAsyncClient.calls = []
    monkeypatch.setattr("tool_broker.main.httpx.AsyncClient", _FakeAsyncClient)
    client = _client(require_auth=True)

    resp = client.post(
        "/api/tool-broker/v1/llm-gateway/v1/chat/completions",
        headers={
            "Authorization": "Bearer sandbox-token",
            "X-Tenant-Id": "tnt_123",
            "X-User-Id": "usr_123",
            "X-Service-Scopes": "llm.admin,evil",
            "X-Tool-Broker-Lease": _lease(["llm-gateway.invoke"]),
        },
        json={"model": "m", "messages": []},
    )

    assert resp.status_code == 200
    forwarded = {k.lower(): v for k, v in _FakeAsyncClient.calls[0]["headers"].items()}
    assert "x-service-scopes" not in forwarded


# ---------------------------------------------------------------------------
# P0-2: fail-closed lease-secret startup guard
# ---------------------------------------------------------------------------


def test_config_rejects_placeholder_lease_secret_outside_dev() -> None:
    import pytest

    with pytest.raises(ValueError, match="TOOL_BROKER_LEASE_SECRET"):
        Settings(env="prod", lease_secret="dev-tool-broker-lease-secret-change-me")


def test_config_rejects_short_lease_secret_outside_dev() -> None:
    import pytest

    with pytest.raises(ValueError, match="TOOL_BROKER_LEASE_SECRET"):
        Settings(env="staging", lease_secret="too-short")


def test_config_accepts_strong_lease_secret_outside_dev() -> None:
    import secrets

    strong = secrets.token_urlsafe(48)
    assert len(strong) >= 32
    settings = Settings(env="prod", lease_secret=strong)
    assert settings.lease_secret == strong


def test_config_allows_placeholder_lease_secret_in_dev() -> None:
    # Dev tolerates the shipped placeholder so the local stack just works.
    settings = Settings(env="dev", lease_secret="dev-tool-broker-lease-secret-change-me")
    assert settings.env == "dev"
