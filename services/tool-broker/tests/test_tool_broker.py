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
    """Records the broker's outgoing upstream request and returns a canned body.

    The broker now obtains the upstream response via ``client.build_request(...)``
    + ``await client.send(request, stream=True)`` so it can inspect the upstream
    Content-Type before deciding whether to buffer or stream.  This fake mirrors
    that surface: ``build_request`` produces a real ``httpx.Request`` (so the
    recorded method/url/headers/content match what the proxy built) and ``send``
    records the call and hands back a buffered JSON response — i.e. a NON-SSE
    response, so the proxy takes the unchanged buffered branch.

    The buffered branch calls ``upstream.aread()`` / ``upstream.aclose()`` /
    ``client.aclose()``; ``aread``/``aclose`` already work on a buffered
    ``httpx.Response`` and ``aclose`` is a no-op on this fake.
    """

    calls: list[dict[str, Any]] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.kwargs = kwargs

    async def __aenter__(self) -> "_FakeAsyncClient":
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def aclose(self) -> None:
        return None

    def build_request(
        self, method: str, url: str, **kwargs: Any
    ) -> httpx.Request:
        request = httpx.Request(
            method,
            url,
            headers=kwargs.get("headers"),
            content=kwargs.get("content"),
        )
        # Record the EXACT headers the broker forwarded (``_forward_headers``
        # output), not the synthesized ``httpx.Request`` headers — httpx adds
        # defaults like Host/Accept/Content-Length that the broker never set, and
        # the security assertions pin that e.g. ``host`` was stripped.
        request.extensions["_broker_forwarded_headers"] = kwargs.get("headers") or {}
        return request

    async def send(
        self, request: httpx.Request, *, stream: bool = False, **kwargs: Any
    ) -> httpx.Response:
        self.calls.append(
            {
                "method": request.method,
                "url": str(request.url),
                "headers": dict(
                    request.extensions.get("_broker_forwarded_headers", {})
                ),
                "content": request.content,
                "stream": stream,
            }
        )
        return httpx.Response(
            200,
            json={"ok": True, "target": str(request.url)},
            headers={"content-type": "application/json", "connection": "close"},
            request=request,
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
        knowledge_ingest_url="http://knowledge-ingest:8012/api/knowledge/v1",
    )
    return TestClient(create_app(settings))


def _lease(
    scopes: list[str],
    *,
    tenant_id: str = "tnt_123",
    user_id: str = "usr_123",
    run_id: str = "run_123",
    clearance: str = "",
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
    if clearance:
        payload["clearance"] = clearance
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
# /dlp façade (foundation-dlp /core/scan)
# ---------------------------------------------------------------------------


def test_dlp_proxy_forwards_core_scan_and_tenant(monkeypatch) -> None:
    """A leased dlp.scan request proxies POST /core/scan to foundation-dlp.

    The broker must (a) forward to the configured DLP upstream base + the
    allowlisted ``/core/scan`` path, and (b) carry X-Tenant-Id (re-injected
    from the validated lease — the DLP service requires it) while still
    stripping the sandbox-supplied identity headers.
    """
    _FakeAsyncClient.calls = []
    monkeypatch.setattr("tool_broker.main.httpx.AsyncClient", _FakeAsyncClient)
    client = _client(require_auth=True)

    resp = client.post(
        "/api/tool-broker/v1/dlp/api/dlp/v1/core/scan",
        headers={
            "Authorization": "Bearer sandbox-token",
            "X-Tenant-Id": "tnt_123",
            "X-User-Id": "usr_123",
            "X-Tool-Broker-Lease": _lease(["dlp.scan"]),
        },
        json={
            "content": "ssn 123-45-6789",
            "resource_type": "tool_call",
            "resource_id": "call_1",
        },
    )

    assert resp.status_code == 200
    assert len(_FakeAsyncClient.calls) == 1
    call = _FakeAsyncClient.calls[0]
    assert call["method"] == "POST"
    # The DLP upstream default is ``http://foundation-dlp:80`` (k8s svc port).
    # httpx.URL canonicalizes away the default HTTP port (:80), so the recorded
    # outbound URL drops it — semantically identical, just normalized.
    assert call["url"] == "http://foundation-dlp/api/dlp/v1/core/scan"
    forwarded = {k.lower(): v for k, v in call["headers"].items()}
    # X-Tenant-Id is re-injected from the lease (DLP requires it).
    assert forwarded["x-tenant-id"] == "tnt_123"
    assert forwarded["authorization"] == "Bearer sandbox-token"


def test_dlp_proxy_wrong_scope_is_rejected(monkeypatch) -> None:
    """A lease WITHOUT dlp.scan must be rejected before any upstream call."""
    _FakeAsyncClient.calls = []
    monkeypatch.setattr("tool_broker.main.httpx.AsyncClient", _FakeAsyncClient)
    client = _client(require_auth=True)

    resp = client.post(
        "/api/tool-broker/v1/dlp/api/dlp/v1/core/scan",
        headers={
            "Authorization": "Bearer sandbox-token",
            "X-Tenant-Id": "tnt_123",
            "X-User-Id": "usr_123",
            "X-Tool-Broker-Lease": _lease(["web_fetch.invoke"]),  # missing dlp.scan
        },
        json={"content": "x", "resource_type": "tool_call", "resource_id": "c"},
    )

    assert resp.status_code == 403
    assert _FakeAsyncClient.calls == []


def test_dlp_proxy_blocks_unlisted_path(monkeypatch) -> None:
    """Only /core/scan is allowlisted on the DLP upstream; anything else 403s."""
    _FakeAsyncClient.calls = []
    monkeypatch.setattr("tool_broker.main.httpx.AsyncClient", _FakeAsyncClient)
    client = _client(require_auth=True)

    resp = client.get(
        "/api/tool-broker/v1/dlp/api/dlp/v1/policies",
        headers={
            "Authorization": "Bearer sandbox-token",
            "X-Tenant-Id": "tnt_123",
            "X-Tool-Broker-Lease": _lease(["dlp.scan"]),
        },
    )

    assert resp.status_code == 403
    assert resp.json()["detail"] == "tool-broker: upstream path not allowed"
    assert _FakeAsyncClient.calls == []


# ---------------------------------------------------------------------------
# /knowledge/v1 façade (knowledge-ingest /search)
# ---------------------------------------------------------------------------


def test_knowledge_proxy_forwards_search_and_identity(monkeypatch) -> None:
    """A leased knowledge.search GET /search proxies to ki with the three
    identity headers re-injected from the signed lease (X-Tenant-Id /
    X-User-Id / X-User-Clearance), and the query string is preserved."""
    _FakeAsyncClient.calls = []
    monkeypatch.setattr("tool_broker.main.httpx.AsyncClient", _FakeAsyncClient)
    client = _client(require_auth=True)

    resp = client.get(
        "/api/tool-broker/v1/knowledge/v1/search?q=contract&k=5&scope=knowledge",
        headers={
            "Authorization": "Bearer sandbox-token",
            "X-Tenant-Id": "tnt_123",
            "X-User-Id": "usr_123",
            "X-Tool-Broker-Lease": _lease(["knowledge.search"], clearance="L3"),
        },
    )

    assert resp.status_code == 200
    assert len(_FakeAsyncClient.calls) == 1
    call = _FakeAsyncClient.calls[0]
    assert call["method"] == "GET"
    assert call["url"] == (
        "http://knowledge-ingest:8012/api/knowledge/v1/search"
        "?q=contract&k=5&scope=knowledge"
    )
    forwarded = {k.lower(): v for k, v in call["headers"].items()}
    # All three identity headers are re-injected from the validated lease.
    assert forwarded["x-tenant-id"] == "tnt_123"
    assert forwarded["x-user-id"] == "usr_123"
    assert forwarded["x-user-clearance"] == "L3"
    assert forwarded["authorization"] == "Bearer sandbox-token"
    # The lease header itself never reaches the upstream.
    assert "x-tool-broker-lease" not in forwarded


def test_knowledge_proxy_clearance_from_lease_not_request(monkeypatch) -> None:
    """X-User-Clearance must come from the SIGNED lease, not the sandbox request.

    A compromised sandbox sends X-User-Clearance: L5 to try to read restricted
    documents; the broker MUST overwrite it with the lease's L2 (the run's
    real effective clearance) so ki enforces the correct visibility gate.
    """
    _FakeAsyncClient.calls = []
    monkeypatch.setattr("tool_broker.main.httpx.AsyncClient", _FakeAsyncClient)
    client = _client(require_auth=True)

    resp = client.get(
        "/api/tool-broker/v1/knowledge/v1/search?q=x",
        headers={
            "Authorization": "Bearer sandbox-token",
            "X-Tenant-Id": "tnt_123",
            "X-User-Id": "usr_123",
            "X-User-Clearance": "L5",  # spoofed - must be overwritten
            "X-Tool-Broker-Lease": _lease(["knowledge.search"], clearance="L2"),
        },
    )

    assert resp.status_code == 200
    forwarded = {k.lower(): v for k, v in _FakeAsyncClient.calls[0]["headers"].items()}
    assert forwarded["x-user-clearance"] == "L2"  # lease value wins


def test_knowledge_proxy_wrong_scope_is_rejected(monkeypatch) -> None:
    """A lease WITHOUT knowledge.search must be rejected before any upstream call."""
    _FakeAsyncClient.calls = []
    monkeypatch.setattr("tool_broker.main.httpx.AsyncClient", _FakeAsyncClient)
    client = _client(require_auth=True)

    resp = client.get(
        "/api/tool-broker/v1/knowledge/v1/search?q=x",
        headers={
            "Authorization": "Bearer sandbox-token",
            "X-Tenant-Id": "tnt_123",
            "X-User-Id": "usr_123",
            "X-Tool-Broker-Lease": _lease(["web_fetch.invoke"]),  # missing knowledge.search
        },
    )

    assert resp.status_code == 403
    assert _FakeAsyncClient.calls == []


def test_knowledge_proxy_blocks_unlisted_path(monkeypatch) -> None:
    """Only /search is allowlisted on the ki upstream; /documents -> 403.

    list_knowledge is inline-only by design (the sandbox tool surfaces just
    search_knowledge), so /documents must not be reachable through the broker.
    """
    _FakeAsyncClient.calls = []
    monkeypatch.setattr("tool_broker.main.httpx.AsyncClient", _FakeAsyncClient)
    client = _client(require_auth=True)

    resp = client.get(
        "/api/tool-broker/v1/knowledge/v1/documents",
        headers={
            "Authorization": "Bearer sandbox-token",
            "X-Tenant-Id": "tnt_123",
            "X-Tool-Broker-Lease": _lease(["knowledge.search"]),
        },
    )

    assert resp.status_code == 403
    assert resp.json()["detail"] == "tool-broker: upstream path not allowed"
    assert _FakeAsyncClient.calls == []


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
        "/api/tool-broker/v1/mcp/v1/internal/mcp-servers",
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
        "/api/tool-broker/v1/mcp/v1/internal/mcp-servers",
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
        "/api/tool-broker/v1/skills/v1/internal/skills/resolve",
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


# ---------------------------------------------------------------------------
# B2: per-route upstream path allowlist
# ---------------------------------------------------------------------------
#
# Each passthrough route must forward ONLY the small set of upstream paths the
# worker's extensions actually call (derived by reading those extensions), and
# 403 ``tool-broker: upstream path not allowed`` for anything else.  The
# allowed-path cases prove a legit call still reaches the (fake) upstream; the
# disallowed cases prove the broker refuses before any upstream request.


def _proxy_client(monkeypatch) -> TestClient:
    """Patch ``httpx.AsyncClient`` with the recording fake and return a client."""
    _FakeAsyncClient.calls = []
    monkeypatch.setattr("tool_broker.main.httpx.AsyncClient", _FakeAsyncClient)
    return _client(require_auth=True)


def test_b2_llm_gateway_allows_chat_completions(monkeypatch) -> None:
    client = _proxy_client(monkeypatch)
    resp = client.post(
        "/api/tool-broker/v1/llm-gateway/v1/chat/completions",
        headers={
            "Authorization": "Bearer sandbox-token",
            "X-Tenant-Id": "tnt_123",
            "X-Tool-Broker-Lease": _lease(["llm-gateway.invoke"]),
        },
        json={"model": "m", "messages": []},
    )
    assert resp.status_code == 200
    assert len(_FakeAsyncClient.calls) == 1


def test_b2_llm_gateway_blocks_unlisted_path(monkeypatch) -> None:
    client = _proxy_client(monkeypatch)
    # The enforcer never calls ``/models`` — only ``/chat/completions``.
    resp = client.get(
        "/api/tool-broker/v1/llm-gateway/v1/models",
        headers={
            "Authorization": "Bearer sandbox-token",
            "X-Tenant-Id": "tnt_123",
            "X-Tool-Broker-Lease": _lease(["llm-gateway.invoke"]),
        },
    )
    assert resp.status_code == 403
    assert resp.json()["detail"] == "tool-broker: upstream path not allowed"
    assert _FakeAsyncClient.calls == []


def test_b2_mcp_allows_internal_mcp_servers(monkeypatch) -> None:
    client = _proxy_client(monkeypatch)
    resp = client.get(
        "/api/tool-broker/v1/mcp/v1/internal/mcp-servers?tenant_id=tnt_123",
        headers={
            "Authorization": "Bearer sandbox-token",
            "X-Tenant-Id": "tnt_123",
            "X-Tool-Broker-Lease": _lease(["mcp.read_internal"]),
        },
    )
    assert resp.status_code == 200
    assert _FakeAsyncClient.calls[0]["url"] == (
        "http://mcp-registry:8010/api/mcp/v1/internal/mcp-servers?tenant_id=tnt_123"
    )


def test_b2_mcp_blocks_unlisted_path(monkeypatch) -> None:
    client = _proxy_client(monkeypatch)
    # ``servers`` (no ``internal/`` prefix) is the broad admin surface — blocked.
    resp = client.post(
        "/api/tool-broker/v1/mcp/v1/servers",
        headers={
            "Authorization": "Bearer sandbox-token",
            "X-Tenant-Id": "tnt_123",
            "X-Tool-Broker-Lease": _lease(["mcp.read_internal"]),
        },
        json={},
    )
    assert resp.status_code == 403
    assert resp.json()["detail"] == "tool-broker: upstream path not allowed"
    assert _FakeAsyncClient.calls == []


def test_b2_skills_allows_internal_skills_prefix(monkeypatch) -> None:
    client = _proxy_client(monkeypatch)
    resp = client.get(
        "/api/tool-broker/v1/skills/v1/internal/skills/resolve?name=foo",
        headers={
            "Authorization": "Bearer sandbox-token",
            "X-Tenant-Id": "tnt_123",
            "X-Tool-Broker-Lease": _lease(["skill.read_internal"]),
        },
    )
    assert resp.status_code == 200
    assert _FakeAsyncClient.calls[0]["url"] == (
        "http://skill-store:8011/api/skills/v1/internal/skills/resolve?name=foo"
    )


def test_b2_skills_blocks_non_internal_path(monkeypatch) -> None:
    client = _proxy_client(monkeypatch)
    # The user-facing ``/skills/{id}`` browse surface is outside the
    # ``internal/skills`` prefix — blocked.
    resp = client.get(
        "/api/tool-broker/v1/skills/v1/skills/my-skill",
        headers={
            "Authorization": "Bearer sandbox-token",
            "X-Tenant-Id": "tnt_123",
            "X-Tool-Broker-Lease": _lease(["skill.read_internal"]),
        },
    )
    assert resp.status_code == 403
    assert resp.json()["detail"] == "tool-broker: upstream path not allowed"
    assert _FakeAsyncClient.calls == []


def test_b2_web_fetch_allows_fetch(monkeypatch) -> None:
    client = _proxy_client(monkeypatch)
    resp = client.post(
        "/api/tool-broker/v1/web-fetch/api/web-fetch/v1/fetch",
        headers={
            "Authorization": "Bearer sandbox-token",
            "X-Tenant-Id": "tnt_123",
            "X-Tool-Broker-Lease": _lease(["web_fetch.invoke"]),
        },
        json={"url": "https://example.com"},
    )
    assert resp.status_code == 200
    assert _FakeAsyncClient.calls[0]["url"] == (
        "http://web-fetch-gateway:8013/api/web-fetch/v1/fetch"
    )


def test_b2_web_fetch_blocks_unlisted_path(monkeypatch) -> None:
    client = _proxy_client(monkeypatch)
    resp = client.get(
        "/api/tool-broker/v1/web-fetch/api/web-fetch/v1/admin/config",
        headers={
            "Authorization": "Bearer sandbox-token",
            "X-Tenant-Id": "tnt_123",
            "X-Tool-Broker-Lease": _lease(["web_fetch.invoke"]),
        },
    )
    assert resp.status_code == 403
    assert resp.json()["detail"] == "tool-broker: upstream path not allowed"
    assert _FakeAsyncClient.calls == []


def test_b2_rejects_path_traversal(monkeypatch) -> None:
    client = _proxy_client(monkeypatch)
    # A ``..`` segment must be rejected even though it would normalise toward an
    # allowed path — defence against climbing out of the upstream surface.
    resp = client.post(
        "/api/tool-broker/v1/mcp/v1/internal/../servers",
        headers={
            "Authorization": "Bearer sandbox-token",
            "X-Tenant-Id": "tnt_123",
            "X-Tool-Broker-Lease": _lease(["mcp.read_internal"]),
        },
        json={},
    )
    assert resp.status_code == 403
    assert resp.json()["detail"] == "tool-broker: upstream path not allowed"
    assert _FakeAsyncClient.calls == []


# ---------------------------------------------------------------------------
# L1: OpenAPI / docs schema endpoints disabled; healthz still reachable
# ---------------------------------------------------------------------------


def test_l1_schema_endpoints_disabled_but_healthz_live() -> None:
    client = _client(require_auth=True)

    for schema_path in ("/openapi.json", "/docs", "/redoc"):
        resp = client.get(schema_path)
        assert resp.status_code == 404, schema_path

    # healthz lives under the api_prefix and stays unauthenticated.
    health = client.get("/api/tool-broker/v1/healthz")
    assert health.status_code == 200
    assert health.json() == {"status": "ok"}


# ---------------------------------------------------------------------------
# L3: the broker must not advertise its server (uvicorn) in responses
# ---------------------------------------------------------------------------


def test_l3_server_header_is_stripped() -> None:
    client = _client(require_auth=True)
    resp = client.get("/api/tool-broker/v1/healthz")
    assert resp.status_code == 200
    # The Server header is popped by the middleware regardless of launcher.
    assert "server" not in {k.lower() for k in resp.headers}


# ---------------------------------------------------------------------------
# SSE pass-through: text/event-stream upstreams must be streamed incrementally
# (per-token LLM streaming), while non-SSE upstreams stay buffered byte-for-byte.
# ---------------------------------------------------------------------------
#
# These exercise the REAL ``_proxy`` streaming path: a genuine ``httpx.Async
# Client`` is wired to an ``httpx.MockTransport`` whose handler decides the
# upstream Content-Type.  Patching ``tool_broker.main.httpx.AsyncClient`` with a
# factory that injects the transport means the broker's own
# ``build_request`` + ``send(stream=True)`` + ``aiter_raw()`` code runs unchanged.

# An SSE upstream emits these as SEPARATE network chunks; the broker must
# deliver them incrementally rather than coalescing into one blob.
_SSE_CHUNKS = [
    b"data: {\"delta\": \"Hel\"}\n\n",
    b"data: {\"delta\": \"lo\"}\n\n",
    b"data: {\"delta\": \" world\"}\n\n",
    b"data: [DONE]\n\n",
]


# Bind the REAL class up front: tests monkeypatch ``tool_broker.main.httpx
# .AsyncClient`` (the shared ``httpx`` module object), so referencing
# ``httpx.AsyncClient`` from inside the factory after patching would resolve to
# the factory itself and recurse.  Capturing it here keeps the genuine client.
_REAL_ASYNC_CLIENT = httpx.AsyncClient


def _mock_transport_client_factory(handler):
    """Return a drop-in for ``httpx.AsyncClient`` backed by ``handler``.

    The broker constructs its client as ``httpx.AsyncClient(timeout=...)``; this
    factory passes those kwargs straight through to a REAL client but adds a
    ``MockTransport`` so the broker's own streaming machinery
    (``build_request`` + ``send(stream=True)`` + ``aiter_raw()``) runs against
    ``handler`` instead of the network.
    """

    def _factory(*args: Any, **kwargs: Any):
        kwargs.setdefault("transport", httpx.MockTransport(handler))
        return _REAL_ASYNC_CLIENT(**kwargs)

    return _factory


def test_sse_upstream_is_streamed_incrementally(monkeypatch) -> None:
    """A ``text/event-stream`` upstream is proxied as a streamed response.

    The broker must (a) return the chunks the upstream emitted, in order and
    fully preserved, and (b) forward upstream status + content-type while still
    stripping hop-by-hop headers.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        async def _stream():
            for chunk in _SSE_CHUNKS:
                yield chunk

        # Passing an async generator as ``content=`` makes httpx wrap it in an
        # async byte stream, so the response body is delivered incrementally
        # (one network chunk per yield) rather than buffered.
        return httpx.Response(
            200,
            headers={
                "content-type": "text/event-stream",
                # hop-by-hop header that must be stripped from the response
                "connection": "keep-alive",
            },
            content=_stream(),
        )

    monkeypatch.setattr(
        "tool_broker.main.httpx.AsyncClient",
        _mock_transport_client_factory(handler),
    )
    client = _client(require_auth=True)

    with client.stream(
        "POST",
        "/api/tool-broker/v1/llm-gateway/v1/chat/completions",
        headers={
            "Authorization": "Bearer sandbox-token",
            "X-Tenant-Id": "tnt_123",
            "X-User-Id": "usr_123",
            "X-Tool-Broker-Lease": _lease(["llm-gateway.invoke"]),
        },
        json={"model": "m", "messages": [], "stream": True},
    ) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        # hop-by-hop response header was stripped by ``_response_headers``.
        assert "connection" not in {k.lower() for k in resp.headers}
        received = list(resp.iter_raw())

    # The full SSE body is preserved (every data: frame, in order). Note: the
    # in-process ``TestClient`` transport may recombine network chunks, so the
    # *count* of pieces here is not a reliable signal of incremental delivery —
    # ``test_sse_response_streams_chunks_incrementally`` pins that directly on
    # the ``StreamingResponse.body_iterator``.  Here we only assert byte-perfect
    # preservation + correct ordering of the full SSE body.
    body = b"".join(received)
    for chunk in _SSE_CHUNKS:
        assert chunk in body
    assert body == b"".join(_SSE_CHUNKS)


def test_sse_response_streams_chunks_incrementally(monkeypatch) -> None:
    """``_proxy`` returns a ``StreamingResponse`` that yields SSE chunks one by one.

    Pins the response TYPE (a future refactor cannot silently collapse SSE back
    into a buffered ``Response``) AND that the body is delivered incrementally:
    draining ``StreamingResponse.body_iterator`` yields the four upstream chunks
    as four separate, in-order pieces — i.e. per-token streaming is preserved,
    not coalesced into one blob.  This asserts at the ASGI-response boundary,
    below the in-process ``TestClient`` transport that recombines chunks.
    """
    import asyncio

    from starlette.requests import Request
    from starlette.responses import StreamingResponse

    from tool_broker.config import Settings
    from tool_broker.main import _proxy

    def handler(request: httpx.Request) -> httpx.Response:
        async def _stream():
            for chunk in _SSE_CHUNKS:
                yield chunk

        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_stream(),
        )

    monkeypatch.setattr(
        "tool_broker.main.httpx.AsyncClient",
        _mock_transport_client_factory(handler),
    )

    settings = Settings(
        require_auth=False,
        require_lease=False,
        enforce_upstream_allowlist=False,
        llm_gateway_url="http://llm-gateway:8011/api/llm-gateway/v1",
    )

    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "path": "/api/tool-broker/v1/llm-gateway/v1/chat/completions",
        "raw_path": b"/api/tool-broker/v1/llm-gateway/v1/chat/completions",
        "query_string": b"",
        "headers": [(b"content-type", b"application/json")],
        "app": type("App", (), {"state": type("S", (), {"settings": settings})()})(),
    }

    async def _receive():
        return {"type": "http.request", "body": b"{}", "more_body": False}

    request = Request(scope, _receive)

    async def _run():
        response = await _proxy(
            request,
            upstream_base_url=settings.llm_gateway_url,
            path="chat/completions",
            required_scope="llm-gateway.invoke",
            upstream_service_scope=None,
        )
        # Drain the body iterator directly to observe incremental delivery.
        chunks = [chunk async for chunk in response.body_iterator]
        return response, chunks

    response, chunks = asyncio.run(_run())
    assert isinstance(response, StreamingResponse)
    assert response.media_type == "text/event-stream"
    # Each upstream SSE frame arrived as its own chunk, in order — not coalesced.
    assert [bytes(c) for c in chunks] == _SSE_CHUNKS


def test_non_sse_upstream_stays_buffered(monkeypatch) -> None:
    """A non-SSE (JSON) upstream is returned as a normal buffered Response.

    This keeps every other route (state/s3 are not proxied, but mcp/skills/
    web-fetch are) byte-for-byte unchanged: same status, same JSON body, same
    content-type, with hop-by-hop headers stripped.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"servers": ["a", "b"]},
            headers={"content-type": "application/json", "connection": "close"},
        )

    monkeypatch.setattr(
        "tool_broker.main.httpx.AsyncClient",
        _mock_transport_client_factory(handler),
    )
    client = _client(require_auth=True)

    resp = client.get(
        "/api/tool-broker/v1/mcp/v1/internal/mcp-servers",
        headers={
            "Authorization": "Bearer sandbox-token",
            "X-Tenant-Id": "tnt_123",
            "X-User-Id": "usr_123",
            "X-Tool-Broker-Lease": _lease(["mcp.read_internal"]),
        },
    )

    assert resp.status_code == 200
    assert resp.json() == {"servers": ["a", "b"]}
    assert resp.headers["content-type"].startswith("application/json")
    # hop-by-hop header stripped on the buffered path too.
    assert "connection" not in {k.lower() for k in resp.headers}


# ---------------------------------------------------------------------------
# /mcp-tenant passthrough: invoke a tenant-registered MCP server by id
# ---------------------------------------------------------------------------
#
# The route makes TWO upstream calls on the happy path: (1) a GET to the
# mcp-registry internal listing to resolve ``server_id`` -> ``endpoint``, then
# (2) the actual MCP JSON-RPC request to that endpoint.  Both go through
# ``httpx.AsyncClient``, so we patch it with the SAME mock-transport factory the
# SSE tests use and let a single ``handler`` dispatch on the request URL.

_MCP_REGISTRY_INTERNAL_PREFIX = (
    "http://mcp-registry:8010/api/mcp/v1/internal/mcp-servers"
)
_TENANT_MCP_SERVER_ID = "mcp_0123456789abcdef0123456789"
_TENANT_MCP_ENDPOINT = "http://echo-mcp:9000/mcp"


def _registry_item(
    *,
    server_id: str = _TENANT_MCP_SERVER_ID,
    tenant_id: str = "tnt_123",
    endpoint: str | None = _TENANT_MCP_ENDPOINT,
    transport: str = "http",
) -> dict[str, Any]:
    return {
        "id": server_id,
        "tenant_id": tenant_id,
        "name": "echo",
        "transport": transport,
        "endpoint": endpoint,
        "command": None,
        "env": {},
        "scopes": [],
        "enabled": True,
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
    }


def _tenant_mcp_handler(
    *,
    items: list[dict[str, Any]],
    upstream_recorder: list[dict[str, Any]],
    registry_recorder: list[dict[str, Any]] | None = None,
    upstream_sse: bool = False,
):
    """Build a MockTransport handler dispatching registry vs. upstream calls."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.startswith(_MCP_REGISTRY_INTERNAL_PREFIX):
            if registry_recorder is not None:
                registry_recorder.append(
                    {
                        "url": url,
                        "headers": {k.lower(): v for k, v in request.headers.items()},
                    }
                )
            return httpx.Response(
                200,
                json={"items": items, "total": len(items)},
                headers={"content-type": "application/json"},
            )
        # Otherwise this is the forward to the resolved tenant MCP endpoint.
        upstream_recorder.append(
            {
                "method": request.method,
                "url": url,
                "headers": {k.lower(): v for k, v in request.headers.items()},
                "content": request.content,
            }
        )
        if upstream_sse:
            async def _stream():
                for chunk in _SSE_CHUNKS:
                    yield chunk

            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=_stream(),
            )
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": 1, "result": {"ok": True}},
            headers={"content-type": "application/json"},
        )

    return handler


def _mcp_tenant_client(handler, monkeypatch) -> TestClient:
    monkeypatch.setattr(
        "tool_broker.main.httpx.AsyncClient",
        _mock_transport_client_factory(handler),
    )
    return _client(require_auth=True)


def test_mcp_tenant_wrong_scope_is_rejected(monkeypatch) -> None:
    """A lease WITHOUT mcp.tenant_invoke is rejected before any upstream call."""
    upstream: list[dict[str, Any]] = []
    registry: list[dict[str, Any]] = []
    handler = _tenant_mcp_handler(
        items=[_registry_item()],
        upstream_recorder=upstream,
        registry_recorder=registry,
    )
    client = _mcp_tenant_client(handler, monkeypatch)

    resp = client.post(
        f"/api/tool-broker/v1/mcp-tenant/v1/{_TENANT_MCP_SERVER_ID}/",
        headers={
            "Authorization": "Bearer sandbox-token",
            "X-Tenant-Id": "tnt_123",
            "X-User-Id": "usr_123",
            # missing mcp.tenant_invoke
            "X-Tool-Broker-Lease": _lease(["mcp.read_internal"]),
        },
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )

    assert resp.status_code == 403
    # Neither the registry lookup nor the upstream forward happened.
    assert registry == []
    assert upstream == []


def test_mcp_tenant_happy_path_forwards_jsonrpc(monkeypatch) -> None:
    """A leased mcp.tenant_invoke request resolves the id and forwards the call.

    Asserts (a) the registry lookup is keyed to the LEASE tenant, (b) the MCP
    JSON-RPC POST reaches the resolved ``endpoint`` verbatim (empty trailing
    path), and (c) the third-party upstream receives NO sandbox identity / lease
    headers.
    """
    upstream: list[dict[str, Any]] = []
    registry: list[dict[str, Any]] = []
    handler = _tenant_mcp_handler(
        items=[_registry_item()],
        upstream_recorder=upstream,
        registry_recorder=registry,
    )
    client = _mcp_tenant_client(handler, monkeypatch)

    resp = client.post(
        f"/api/tool-broker/v1/mcp-tenant/v1/{_TENANT_MCP_SERVER_ID}/",
        headers={
            "Authorization": "Bearer sandbox-token",
            "X-Tenant-Id": "tnt_123",
            "X-User-Id": "usr_123",
            # Sandbox tries to smuggle privilege-escalation headers.
            "X-Actor": "ADMIN",
            "X-Service-Scopes": "mcp.admin",
            "X-Tool-Broker-Lease": _lease(["mcp.tenant_invoke"]),
        },
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )

    assert resp.status_code == 200
    assert resp.json()["result"] == {"ok": True}

    # (a) registry lookup keyed to the lease tenant + presenting the trusted
    # service scope and the matching tenant header.
    assert len(registry) == 1
    assert registry[0]["url"] == (
        f"{_MCP_REGISTRY_INTERNAL_PREFIX}?tenant_id=tnt_123"
    )
    assert registry[0]["headers"]["x-service-scopes"] == "mcp.read_internal"
    assert registry[0]["headers"]["x-tenant-id"] == "tnt_123"

    # (b) the MCP request reached the resolved endpoint verbatim.
    assert len(upstream) == 1
    call = upstream[0]
    assert call["method"] == "POST"
    assert call["url"] == _TENANT_MCP_ENDPOINT

    # (c) third-party upstream gets NO sandbox identity / lease headers, and
    # CRUCIALLY no foundation Bearer (a tenant-bound service-account JWT a
    # malicious tenant MCP server could replay against foundation services).
    fwd = call["headers"]
    assert "x-actor" not in fwd
    assert "x-service-scopes" not in fwd
    assert "x-user-id" not in fwd
    assert "x-tenant-id" not in fwd
    assert "x-tool-broker-lease" not in fwd
    assert "authorization" not in fwd


def test_mcp_tenant_injects_registry_auth_and_headers_toward_upstream(
    monkeypatch,
) -> None:
    """The broker is the single upstream-auth injection point for broker-routed
    sessions: registry-supplied ``auth`` + ``headers`` reach the real upstream,
    while the sandbox's foundation Bearer is replaced (not leaked).
    """
    upstream: list[dict[str, Any]] = []
    item = _registry_item()
    item["headers"] = {"X-Custom": "reg"}
    item["auth"] = {"type": "bearer", "token": "upstream-tok"}
    handler = _tenant_mcp_handler(items=[item], upstream_recorder=upstream)
    client = _mcp_tenant_client(handler, monkeypatch)

    resp = client.post(
        f"/api/tool-broker/v1/mcp-tenant/v1/{_TENANT_MCP_SERVER_ID}/",
        headers={
            "Authorization": "Bearer sandbox-token",
            "X-Tenant-Id": "tnt_123",
            "X-User-Id": "usr_123",
            "X-Tool-Broker-Lease": _lease(["mcp.tenant_invoke"]),
        },
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )

    assert resp.status_code == 200
    assert len(upstream) == 1
    fwd = upstream[0]["headers"]
    # Registry custom header + bearer auth injected toward the real upstream.
    assert fwd["x-custom"] == "reg"
    assert fwd["authorization"] == "Bearer upstream-tok"
    # The sandbox's foundation Bearer is replaced by the registry's upstream
    # bearer (the ONLY Authorization on the upstream call). No sandbox identity
    # / lease headers reach the third-party upstream.
    assert "x-tenant-id" not in fwd
    assert "x-tool-broker-lease" not in fwd
    assert "x-user-id" not in fwd


def test_mcp_tenant_no_trailing_path_matches_worker_target(monkeypatch) -> None:
    """The real worker target has NO trailing path: ``.../{server_id}``.

    The SDK's streamable-http transport POSTs to the base URL with no sub-path,
    so the captured ``{path}`` is empty and the broker must forward to the
    resolved ``endpoint`` verbatim.

    REGRESSION GUARD: the bare path must match DIRECTLY, with NO 307 redirect to
    the trailing-slash form. ``follow_redirects=False`` makes any redirect
    surface as a 307 here. In the real sandbox the MCP client's fetch/undici
    DROPS the ``Authorization``/``x-tool-broker-lease`` headers across such a
    redirect, so the redirected request hits the broker unauthenticated → 401
    and the tool never surfaces. Asserting a direct 200 (not 307) pins the fix.
    """
    upstream: list[dict[str, Any]] = []
    handler = _tenant_mcp_handler(
        items=[_registry_item()],
        upstream_recorder=upstream,
    )
    client = _mcp_tenant_client(handler, monkeypatch)

    # NOTE: no trailing slash after the server id — exactly what the worker
    # points the SDK at.  follow_redirects=False so a 307 would fail the test
    # instead of being transparently followed (and re-sent with headers, which
    # undici would NOT do).
    resp = client.post(
        f"/api/tool-broker/v1/mcp-tenant/v1/{_TENANT_MCP_SERVER_ID}",
        headers={
            "Authorization": "Bearer sandbox-token",
            "X-Tenant-Id": "tnt_123",
            "X-Tool-Broker-Lease": _lease(["mcp.tenant_invoke"]),
        },
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        follow_redirects=False,
    )

    assert resp.status_code == 200, (
        f"bare path must match directly without a redirect; got {resp.status_code} "
        f"(307 means redirect_slashes would strip the auth/lease headers in undici)"
    )
    assert len(upstream) == 1
    # Forwarded to the resolved endpoint verbatim (no spurious trailing slash)
    # as a POST — i.e. matched the route directly, not via a body-losing
    # redirect that would have downgraded the method.
    assert upstream[0]["method"] == "POST"
    assert upstream[0]["url"] == _TENANT_MCP_ENDPOINT
    # The body is forwarded verbatim.  httpx (the TestClient) serialises JSON
    # compactly (no whitespace after ``,``/``:``), so compare against that exact
    # encoding rather than ``json.dumps``'s default spaced separators.
    assert upstream[0]["content"] == json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        separators=(",", ":"),
    ).encode()


def test_mcp_tenant_unknown_server_id_returns_404(monkeypatch) -> None:
    """A registry list with no matching id yields 404 (worker drops the server)."""
    upstream: list[dict[str, Any]] = []
    handler = _tenant_mcp_handler(
        items=[_registry_item(server_id="mcp_someotherserveridxxxxxxxxx")],
        upstream_recorder=upstream,
    )
    client = _mcp_tenant_client(handler, monkeypatch)

    resp = client.post(
        f"/api/tool-broker/v1/mcp-tenant/v1/{_TENANT_MCP_SERVER_ID}/",
        headers={
            "Authorization": "Bearer sandbox-token",
            "X-Tenant-Id": "tnt_123",
            "X-Tool-Broker-Lease": _lease(["mcp.tenant_invoke"]),
        },
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )

    assert resp.status_code == 404
    assert resp.json()["detail"] == "tool-broker: mcp server not found"
    assert upstream == []


def test_mcp_tenant_empty_registry_returns_404(monkeypatch) -> None:
    """An empty registry listing (disabled/absent server) yields 404."""
    upstream: list[dict[str, Any]] = []
    handler = _tenant_mcp_handler(items=[], upstream_recorder=upstream)
    client = _mcp_tenant_client(handler, monkeypatch)

    resp = client.post(
        f"/api/tool-broker/v1/mcp-tenant/v1/{_TENANT_MCP_SERVER_ID}/",
        headers={
            "Authorization": "Bearer sandbox-token",
            "X-Tenant-Id": "tnt_123",
            "X-Tool-Broker-Lease": _lease(["mcp.tenant_invoke"]),
        },
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )

    assert resp.status_code == 404
    assert upstream == []


def test_mcp_tenant_stdio_server_without_endpoint_returns_404(monkeypatch) -> None:
    """A stdio server (no HTTP endpoint) cannot be reached -> 404."""
    upstream: list[dict[str, Any]] = []
    handler = _tenant_mcp_handler(
        items=[_registry_item(endpoint=None, transport="stdio")],
        upstream_recorder=upstream,
    )
    client = _mcp_tenant_client(handler, monkeypatch)

    resp = client.post(
        f"/api/tool-broker/v1/mcp-tenant/v1/{_TENANT_MCP_SERVER_ID}/",
        headers={
            "Authorization": "Bearer sandbox-token",
            "X-Tenant-Id": "tnt_123",
            "X-Tool-Broker-Lease": _lease(["mcp.tenant_invoke"]),
        },
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )

    assert resp.status_code == 404
    assert upstream == []


def test_mcp_tenant_lookup_uses_lease_tenant_not_request_header(monkeypatch) -> None:
    """The registry lookup tenant comes from the LEASE, not a sandbox header.

    The request lease guard already forces the declared X-Tenant-Id to match the
    lease, but this pins that the registry query param + X-Tenant-Id sent to the
    registry are the lease tenant (so a sandbox can never enumerate / reach
    another tenant's servers).
    """
    upstream: list[dict[str, Any]] = []
    registry: list[dict[str, Any]] = []
    # The server belongs to tnt_123 (the lease tenant); the handler returns it
    # only when the registry is queried for tnt_123.
    handler = _tenant_mcp_handler(
        items=[_registry_item(tenant_id="tnt_123")],
        upstream_recorder=upstream,
        registry_recorder=registry,
    )
    client = _mcp_tenant_client(handler, monkeypatch)

    resp = client.post(
        f"/api/tool-broker/v1/mcp-tenant/v1/{_TENANT_MCP_SERVER_ID}/",
        headers={
            "Authorization": "Bearer sandbox-token",
            "X-Tenant-Id": "tnt_123",
            "X-Tool-Broker-Lease": _lease(["mcp.tenant_invoke"], tenant_id="tnt_123"),
        },
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )

    assert resp.status_code == 200
    assert len(registry) == 1
    assert registry[0]["url"].endswith("?tenant_id=tnt_123")
    assert registry[0]["headers"]["x-tenant-id"] == "tnt_123"


def test_mcp_tenant_allows_delete_method(monkeypatch) -> None:
    """DELETE is allowed on the tenant-MCP route (streamable-http teardown)."""
    upstream: list[dict[str, Any]] = []
    handler = _tenant_mcp_handler(
        items=[_registry_item()],
        upstream_recorder=upstream,
    )
    client = _mcp_tenant_client(handler, monkeypatch)

    resp = client.delete(
        f"/api/tool-broker/v1/mcp-tenant/v1/{_TENANT_MCP_SERVER_ID}/",
        headers={
            "Authorization": "Bearer sandbox-token",
            "X-Tenant-Id": "tnt_123",
            "X-Tool-Broker-Lease": _lease(["mcp.tenant_invoke"]),
        },
    )

    assert resp.status_code == 200
    assert len(upstream) == 1
    assert upstream[0]["method"] == "DELETE"
    assert upstream[0]["url"] == _TENANT_MCP_ENDPOINT


def test_existing_route_still_rejects_delete(monkeypatch) -> None:
    """The foundation-internal routes must NOT start accepting DELETE.

    Widening DELETE for the tenant-MCP route must not leak into the shared
    ``_ALLOWED_METHODS`` guard the other proxy routes use.
    """
    _FakeAsyncClient.calls = []
    monkeypatch.setattr("tool_broker.main.httpx.AsyncClient", _FakeAsyncClient)
    client = _client(require_auth=True)

    resp = client.delete(
        "/api/tool-broker/v1/mcp/v1/internal/mcp-servers",
        headers={
            "Authorization": "Bearer sandbox-token",
            "X-Tenant-Id": "tnt_123",
            "X-Tool-Broker-Lease": _lease(["mcp.read_internal"]),
        },
    )

    # DELETE is not registered on the existing mcp route -> 405 Method Not
    # Allowed, and no upstream call.
    assert resp.status_code == 405
    assert _FakeAsyncClient.calls == []


def test_mcp_tenant_streams_sse_upstream(monkeypatch) -> None:
    """A tenant MCP server returning text/event-stream is streamed through."""
    upstream: list[dict[str, Any]] = []
    handler = _tenant_mcp_handler(
        items=[_registry_item()],
        upstream_recorder=upstream,
        upstream_sse=True,
    )
    client = _mcp_tenant_client(handler, monkeypatch)

    with client.stream(
        "POST",
        f"/api/tool-broker/v1/mcp-tenant/v1/{_TENANT_MCP_SERVER_ID}/",
        headers={
            "Authorization": "Bearer sandbox-token",
            "X-Tenant-Id": "tnt_123",
            "X-Tool-Broker-Lease": _lease(["mcp.tenant_invoke"]),
        },
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call"},
    ) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        body = b"".join(resp.iter_raw())

    assert body == b"".join(_SSE_CHUNKS)


def test_proxy_upstream_connection_error_returns_502(monkeypatch) -> None:
    """A connect-time upstream failure still maps to 502 (unchanged behaviour)."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("upstream down", request=request)

    monkeypatch.setattr(
        "tool_broker.main.httpx.AsyncClient",
        _mock_transport_client_factory(handler),
    )
    client = _client(require_auth=True)

    resp = client.post(
        "/api/tool-broker/v1/llm-gateway/v1/chat/completions",
        headers={
            "Authorization": "Bearer sandbox-token",
            "X-Tenant-Id": "tnt_123",
            "X-User-Id": "usr_123",
            "X-Tool-Broker-Lease": _lease(["llm-gateway.invoke"]),
        },
        json={"model": "m", "messages": []},
    )

    assert resp.status_code == 502
    assert resp.json()["detail"] == "tool-broker upstream unavailable"


# ---------------------------------------------------------------------------
# OAuth client-credentials auth (tenant MCP servers)
# ---------------------------------------------------------------------------
#
# When a tenant MCP server's ``auth.type == "oauth"`` the broker performs the
# client-credentials flow itself (POST to the registry-supplied ``token_url``),
# caches the access_token by server_id, and injects ``Authorization: Bearer
# {access_token}`` toward the real upstream.  These tests exercise the full
# /mcp-tenant passthrough (registry lookup -> token fetch -> upstream forward)
# via a single MockTransport handler that dispatches on the request URL, plus
# direct unit tests for the cache / single-flight / refresh logic.

_OAUTH_TOKEN_URL = "https://idp.example/oauth/token"


def _reset_oauth_cache() -> None:
    """Clear the module-level oauth token cache + single-flight locks."""
    from tool_broker.main import _OAUTH_LOCKS, _OAUTH_TOKEN_CACHE

    _OAUTH_TOKEN_CACHE.clear()
    _OAUTH_LOCKS.clear()


def _oauth_registry_item(
    *,
    server_id: str = _TENANT_MCP_SERVER_ID,
    scopes: list[str] | None = None,
) -> dict[str, Any]:
    item = _registry_item(server_id=server_id)
    auth: dict[str, Any] = {
        "type": "oauth",
        "client_id": "cid",
        "client_secret": "csec",
        "token_url": _OAUTH_TOKEN_URL,
    }
    if scopes is not None:
        auth["scopes"] = scopes
    item["auth"] = auth
    return item


def _oauth_dispatch_handler(
    *,
    item: dict[str, Any],
    token_calls: list[dict[str, Any]],
    upstream: list[dict[str, Any]],
    token_status: int = 200,
    token_body: dict[str, Any] | None = None,
):
    """MockTransport handler dispatching registry / token-endpoint / upstream."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == _OAUTH_TOKEN_URL:
            from urllib.parse import parse_qs

            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            token_calls.append({"url": url, "form": form})
            if token_status != 200:
                return httpx.Response(
                    token_status, json={"error": "denied"}, headers={"content-type": "application/json"}
                )
            body = token_body or {"access_token": "tok-abc", "expires_in": 3600}
            return httpx.Response(
                200, json=body, headers={"content-type": "application/json"}
            )
        if url.startswith(_MCP_REGISTRY_INTERNAL_PREFIX):
            return httpx.Response(
                200,
                json={"items": [item], "total": 1},
                headers={"content-type": "application/json"},
            )
        upstream.append(
            {
                "method": request.method,
                "url": url,
                "headers": {k.lower(): v for k, v in request.headers.items()},
            }
        )
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": 1, "result": {"ok": True}},
            headers={"content-type": "application/json"},
        )

    return handler


def _mcp_tenant_headers() -> dict[str, str]:
    return {
        "Authorization": "Bearer sandbox-token",
        "X-Tenant-Id": "tnt_123",
        "X-User-Id": "usr_123",
        "X-Tool-Broker-Lease": _lease(["mcp.tenant_invoke"]),
    }


def test_mcp_tenant_oauth_fetches_token_and_injects_bearer(monkeypatch) -> None:
    """oauth auth: broker POSTs client_credentials to token_url and injects the
    resulting bearer toward the real upstream."""
    _reset_oauth_cache()
    token_calls: list[dict[str, Any]] = []
    upstream: list[dict[str, Any]] = []
    handler = _oauth_dispatch_handler(
        item=_oauth_registry_item(scopes=["a", "b"]),
        token_calls=token_calls,
        upstream=upstream,
    )
    client = _mcp_tenant_client(handler, monkeypatch)

    resp = client.post(
        f"/api/tool-broker/v1/mcp-tenant/v1/{_TENANT_MCP_SERVER_ID}/",
        headers=_mcp_tenant_headers(),
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )

    assert resp.status_code == 200
    # The client_credentials form was POSTed to the token endpoint once.
    assert len(token_calls) == 1
    form = token_calls[0]["form"]
    assert form["grant_type"] == "client_credentials"
    assert form["client_id"] == "cid"
    assert form["client_secret"] == "csec"
    # scopes are space-joined into a single ``scope`` form field.
    assert form["scope"] == "a b"
    # The resulting access_token reaches the real upstream as a Bearer.
    assert len(upstream) == 1
    assert upstream[0]["headers"]["authorization"] == "Bearer tok-abc"


def test_mcp_tenant_oauth_omits_scope_when_empty(monkeypatch) -> None:
    """No ``scope`` form field when the registry auth carries no scopes."""
    _reset_oauth_cache()
    token_calls: list[dict[str, Any]] = []
    upstream: list[dict[str, Any]] = []
    handler = _oauth_dispatch_handler(
        item=_oauth_registry_item(scopes=None),
        token_calls=token_calls,
        upstream=upstream,
    )
    client = _mcp_tenant_client(handler, monkeypatch)

    resp = client.post(
        f"/api/tool-broker/v1/mcp-tenant/v1/{_TENANT_MCP_SERVER_ID}/",
        headers=_mcp_tenant_headers(),
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )

    assert resp.status_code == 200
    assert "scope" not in token_calls[0]["form"]
    assert upstream[0]["headers"]["authorization"] == "Bearer tok-abc"


def test_mcp_tenant_oauth_caches_token_across_requests(monkeypatch) -> None:
    """Two requests for the same server_id hit the token endpoint ONCE."""
    _reset_oauth_cache()
    token_calls: list[dict[str, Any]] = []
    upstream: list[dict[str, Any]] = []
    handler = _oauth_dispatch_handler(
        item=_oauth_registry_item(),
        token_calls=token_calls,
        upstream=upstream,
    )
    client = _mcp_tenant_client(handler, monkeypatch)

    for _ in range(2):
        resp = client.post(
            f"/api/tool-broker/v1/mcp-tenant/v1/{_TENANT_MCP_SERVER_ID}/",
            headers=_mcp_tenant_headers(),
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )
        assert resp.status_code == 200

    # The token was fetched once and reused for the second request.
    assert len(token_calls) == 1
    assert len(upstream) == 2
    assert all(call["headers"]["authorization"] == "Bearer tok-abc" for call in upstream)


def test_mcp_tenant_oauth_fetch_failure_is_fail_open(monkeypatch) -> None:
    """A token-endpoint error is fail-open: no Authorization toward upstream."""
    _reset_oauth_cache()
    token_calls: list[dict[str, Any]] = []
    upstream: list[dict[str, Any]] = []
    handler = _oauth_dispatch_handler(
        item=_oauth_registry_item(),
        token_calls=token_calls,
        upstream=upstream,
        token_status=500,
    )
    client = _mcp_tenant_client(handler, monkeypatch)

    resp = client.post(
        f"/api/tool-broker/v1/mcp-tenant/v1/{_TENANT_MCP_SERVER_ID}/",
        headers=_mcp_tenant_headers(),
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )

    # The broker still proxies the request (fail-open), just without the bearer.
    assert resp.status_code == 200
    assert len(token_calls) == 1
    assert len(upstream) == 1
    assert "authorization" not in upstream[0]["headers"]


def test_oauth_get_token_single_flight(monkeypatch) -> None:
    """Concurrent _get_oauth_token calls for one server_id share ONE fetch."""
    import asyncio

    from tool_broker.main import _get_oauth_token

    _reset_oauth_cache()
    calls = 0

    async def fake_fetch(auth):  # noqa: ANN001
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.05)  # simulate token-endpoint latency
        return "tok", time.time() + 3600

    monkeypatch.setattr("tool_broker.main._fetch_oauth_token", fake_fetch)

    auth = {"type": "oauth", "client_id": "c", "client_secret": "s", "token_url": "u"}

    async def run() -> list[str]:
        return await asyncio.gather(
            _get_oauth_token("srv_sf", auth),
            _get_oauth_token("srv_sf", auth),
            _get_oauth_token("srv_sf", auth),
        )

    results = asyncio.run(run())
    assert calls == 1  # single-flight: one fetch shared across three callers
    assert results == ["tok", "tok", "tok"]


def test_oauth_get_token_refreshes_after_expiry(monkeypatch) -> None:
    """A cached token past its refresh margin is refetched."""
    import asyncio

    from tool_broker.main import _get_oauth_token

    _reset_oauth_cache()
    calls = 0

    async def fake_fetch(auth):  # noqa: ANN001
        nonlocal calls
        calls += 1
        # Expires in the past -> always past the refresh margin.
        return f"tok-{calls}", time.time() - 1

    monkeypatch.setattr("tool_broker.main._fetch_oauth_token", fake_fetch)

    auth = {"type": "oauth", "client_id": "c", "client_secret": "s", "token_url": "u"}

    async def run() -> tuple[str, str]:
        first = await _get_oauth_token("srv_refresh", auth)
        second = await _get_oauth_token("srv_refresh", auth)
        return first, second

    first, second = asyncio.run(run())
    assert calls == 2  # second call refetched because the cached token expired
    assert first == "tok-1"
    assert second == "tok-2"


def test_oauth_get_token_uses_cached_valid_token(monkeypatch) -> None:
    """A cached token within its refresh margin is reused without refetch."""
    import asyncio

    from tool_broker.main import _get_oauth_token

    _reset_oauth_cache()
    calls = 0

    async def fake_fetch(auth):  # noqa: ANN001
        nonlocal calls
        calls += 1
        return f"tok-{calls}", time.time() + 3600

    monkeypatch.setattr("tool_broker.main._fetch_oauth_token", fake_fetch)

    auth = {"type": "oauth", "client_id": "c", "client_secret": "s", "token_url": "u"}

    async def run() -> tuple[str, str]:
        first = await _get_oauth_token("srv_cache", auth)
        second = await _get_oauth_token("srv_cache", auth)
        return first, second

    first, second = asyncio.run(run())
    assert calls == 1  # second call reused the cached token
    assert first == second == "tok-1"


def test_oauth_fetch_token_defaults_expires_in(monkeypatch) -> None:
    """A missing/non-numeric ``expires_in`` defaults to 3600s (no crash)."""
    import asyncio

    from tool_broker.main import _fetch_oauth_token

    captured: dict[str, Any] = {}

    class _FakeResp:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {"access_token": "tok"}  # no expires_in

    class _FakeClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.kwargs = kwargs

        async def __aenter__(self) -> "_FakeClient":
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def post(self, url: str, *, data: dict[str, str]) -> _FakeResp:
            captured["url"] = url
            captured["data"] = data
            return _FakeResp()

    monkeypatch.setattr("tool_broker.main.httpx.AsyncClient", _FakeClient)

    auth = {
        "type": "oauth",
        "client_id": "c",
        "client_secret": "s",
        "token_url": "https://idp.example/token",
    }
    token, expires_at = asyncio.run(_fetch_oauth_token(auth))
    assert token == "tok"
    assert captured["url"] == "https://idp.example/token"
    assert captured["data"]["grant_type"] == "client_credentials"
    assert captured["data"]["client_id"] == "c"
    assert captured["data"]["client_secret"] == "s"
    # expires_at is finite (default 3600s from now) even with no expires_in.
    assert expires_at > time.time()

