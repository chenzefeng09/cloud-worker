"""HTTP-layer tests for the web-fetch-gateway POST /fetch endpoint.

Because respx is not available in this environment we mock httpx at the
transport level using httpx.MockTransport / monkeypatching the AsyncClient
constructor in fetcher.py.
"""

from __future__ import annotations

import socket
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from web_fetch_gateway.config import Settings
from web_fetch_gateway.main import create_app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _settings(**overrides: object) -> Settings:
    """Create a test Settings instance with sensible defaults."""
    defaults: dict[str, object] = {
        "env": "test",
        "allowed_domains": "*",  # open for most tests; override per-test
        "max_response_bytes": 1024,
        "fetch_timeout_seconds": 5.0,
        "restrict_content_types": True,
    }
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[arg-type]


def _auth_headers(
    *,
    token: str = "tok-web",
    tenant_id: str = "tnt_01J5WEBFETCH0000000000001",
    user_id: str = "usr_01J5WEBFETCH0000000000001",
    scopes: str = "mcp.read_internal,skill.read_internal,web_fetch.invoke",
) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "X-Tenant-Id": tenant_id,
        "X-User-Id": user_id,
        "X-Service-Scopes": scopes,
    }


def _make_response(
    *,
    status_code: int = 200,
    content_type: str = "text/html; charset=utf-8",
    body: bytes = b"<html>hello</html>",
    is_redirect: bool = False,
) -> httpx.Response:
    """Build a fake httpx.Response."""
    headers = httpx.Headers({"content-type": content_type})
    if is_redirect:
        headers = httpx.Headers({
            "content-type": content_type,
            "location": "https://example.com/redirected",
        })
    resp = httpx.Response(
        status_code=status_code,
        headers=headers,
        content=body,
    )
    return resp


class _MockAsyncClient:
    """Drop-in replacement for httpx.AsyncClient that returns canned responses."""

    def __init__(self, responses: list[httpx.Response], **_kw: object) -> None:
        self._responses = list(responses)
        self._index = 0

    async def __aenter__(self) -> "_MockAsyncClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        pass

    async def request(self, **_kw: object) -> httpx.Response:
        if self._index >= len(self._responses):
            raise AssertionError("MockAsyncClient: no more canned responses")
        resp = self._responses[self._index]
        self._index += 1
        return resp

    async def aclose(self) -> None:
        pass


def _patch_client(responses: list[httpx.Response], monkeypatch: pytest.MonkeyPatch) -> None:
    """Monkeypatch fetcher.httpx.AsyncClient with _MockAsyncClient."""

    def _factory(*args: object, **kwargs: object) -> _MockAsyncClient:
        return _MockAsyncClient(responses, **kwargs)

    monkeypatch.setattr("web_fetch_gateway.fetcher.httpx.AsyncClient", _factory)


def _patch_dns_public(monkeypatch: pytest.MonkeyPatch, ip: str = "93.184.216.34") -> None:
    """Monkeypatch socket.getaddrinfo to return a public IP, bypassing real DNS."""
    monkeypatch.setattr(
        "socket.getaddrinfo",
        lambda host, port, *a, **kw: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 80))
        ],
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Default test client: wildcard allowed_domains, DNS always returns public IP."""
    _patch_dns_public(monkeypatch)
    app = create_app(_settings())
    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# Health endpoint smoke test
# ---------------------------------------------------------------------------

def test_healthz(client: TestClient) -> None:
    resp = client.get("/api/web-fetch/v1/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "OK"
    assert body["service"] == "web-fetch-gateway"


def test_readyz(client: TestClient) -> None:
    resp = client.get("/api/web-fetch/v1/readyz")
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Authentication / service scope
# ---------------------------------------------------------------------------

def test_fetch_requires_authorization_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_dns_public(monkeypatch)
    app = create_app(_settings())
    with TestClient(app, raise_server_exceptions=False) as tc:
        resp = tc.post(
            "/api/web-fetch/v1/fetch",
            json={"url": "https://example.com/"},
        )
    assert resp.status_code == 401
    assert resp.json()["code"] == "FETCH.AUTH_REQUIRED"


def test_fetch_requires_web_fetch_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_dns_public(monkeypatch)
    app = create_app(_settings())
    with TestClient(app, raise_server_exceptions=False) as tc:
        resp = tc.post(
            "/api/web-fetch/v1/fetch",
            headers=_auth_headers(scopes="mcp.read_internal,skill.read_internal"),
            json={"url": "https://example.com/"},
        )
    assert resp.status_code == 403
    assert resp.json()["code"] == "FETCH.SCOPE_REQUIRED"


# ---------------------------------------------------------------------------
# Successful fetch
# ---------------------------------------------------------------------------

def test_successful_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_dns_public(monkeypatch)
    _patch_client(
        [_make_response(body=b"<html>hello world</html>")],
        monkeypatch,
    )
    app = create_app(_settings())
    with TestClient(app, raise_server_exceptions=False) as tc:
        resp = tc.post(
            "/api/web-fetch/v1/fetch",
            headers=_auth_headers(),
            json={"url": "https://example.com/"},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status_code"] == 200
    assert body["body"] == "<html>hello world</html>"
    assert body["truncated"] is False
    assert body["error"] is None


# ---------------------------------------------------------------------------
# Truncation
# ---------------------------------------------------------------------------

def test_body_truncated_when_over_max_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    big_body = b"A" * 2000  # exceeds default max_response_bytes=1024
    _patch_dns_public(monkeypatch)
    _patch_client([_make_response(body=big_body)], monkeypatch)
    app = create_app(_settings(max_response_bytes=1024))
    with TestClient(app, raise_server_exceptions=False) as tc:
        resp = tc.post(
            "/api/web-fetch/v1/fetch",
            headers=_auth_headers(),
            json={"url": "https://example.com/big"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["truncated"] is True
    assert len(body["body"]) == 1024


def test_per_request_max_bytes_bounded_by_server_max(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Caller can set a smaller max_bytes but cannot exceed server max."""
    _patch_dns_public(monkeypatch)
    _patch_client([_make_response(body=b"X" * 500)], monkeypatch)
    # Server max is 1024, caller requests 200 — effective cap should be 200.
    app = create_app(_settings(max_response_bytes=1024))
    with TestClient(app, raise_server_exceptions=False) as tc:
        resp = tc.post(
            "/api/web-fetch/v1/fetch",
            headers=_auth_headers(),
            json={"url": "https://example.com/", "max_bytes": 200},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["truncated"] is True
    assert len(body["body"]) == 200


def test_per_request_max_bytes_cannot_exceed_server_max(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Caller requesting more than server max gets server max applied."""
    _patch_dns_public(monkeypatch)
    _patch_client([_make_response(body=b"Y" * 2000)], monkeypatch)
    # Server max is 512; caller asks for 2000 — server cap wins.
    app = create_app(_settings(max_response_bytes=512))
    with TestClient(app, raise_server_exceptions=False) as tc:
        resp = tc.post(
            "/api/web-fetch/v1/fetch",
            headers=_auth_headers(),
            json={"url": "https://example.com/", "max_bytes": 2000},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["truncated"] is True
    assert len(body["body"]) == 512


# ---------------------------------------------------------------------------
# Timeout → 504
# ---------------------------------------------------------------------------

def test_timeout_returns_504(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_dns_public(monkeypatch)

    async def _slow_request(**_kw: object) -> httpx.Response:  # noqa: ARG001
        raise httpx.TimeoutException("timed out")

    class _TimeoutClient(_MockAsyncClient):
        def __init__(self, **_kw: object) -> None:
            super().__init__([])

        async def request(self, **kw: object) -> httpx.Response:
            raise httpx.TimeoutException("timed out")

    monkeypatch.setattr(
        "web_fetch_gateway.fetcher.httpx.AsyncClient",
        lambda *a, **kw: _TimeoutClient(),
    )

    app = create_app(_settings())
    with TestClient(app, raise_server_exceptions=False) as tc:
        resp = tc.post(
            "/api/web-fetch/v1/fetch",
            headers=_auth_headers(),
            json={"url": "https://example.com/slow"},
        )
    assert resp.status_code == 504
    body = resp.json()
    assert body["code"] == "FETCH.TIMEOUT"


# ---------------------------------------------------------------------------
# Upstream errors → 502
# ---------------------------------------------------------------------------

def test_upstream_request_error_returns_502(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_dns_public(monkeypatch)

    class _ErrorClient(_MockAsyncClient):
        def __init__(self, **_kw: object) -> None:
            super().__init__([])

        async def request(self, **kw: object) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(
        "web_fetch_gateway.fetcher.httpx.AsyncClient",
        lambda *a, **kw: _ErrorClient(),
    )
    app = create_app(_settings())
    with TestClient(app, raise_server_exceptions=False) as tc:
        resp = tc.post(
            "/api/web-fetch/v1/fetch",
            headers=_auth_headers(),
            json={"url": "https://example.com/"},
        )
    assert resp.status_code == 502
    assert resp.json()["code"] == "FETCH.UPSTREAM_ERROR"


# ---------------------------------------------------------------------------
# Content-type rejection → 415
# ---------------------------------------------------------------------------

def test_content_type_rejection_returns_415(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_dns_public(monkeypatch)
    _patch_client(
        [_make_response(content_type="image/png", body=b"\x89PNG")],
        monkeypatch,
    )
    app = create_app(_settings(restrict_content_types=True))
    with TestClient(app, raise_server_exceptions=False) as tc:
        resp = tc.post(
            "/api/web-fetch/v1/fetch",
            headers=_auth_headers(),
            json={"url": "https://example.com/image.png"},
        )
    assert resp.status_code == 415
    assert resp.json()["code"] == "FETCH.CONTENT_TYPE_REJECTED"


def test_content_type_restriction_disabled_allows_binary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_dns_public(monkeypatch)
    _patch_client(
        [_make_response(content_type="image/png", body=b"\x89PNG")],
        monkeypatch,
    )
    app = create_app(_settings(restrict_content_types=False))
    with TestClient(app, raise_server_exceptions=False) as tc:
        resp = tc.post(
            "/api/web-fetch/v1/fetch",
            headers=_auth_headers(),
            json={"url": "https://example.com/image.png"},
        )
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Domain not in allowlist → 403
# ---------------------------------------------------------------------------

def test_domain_not_allowed_returns_403(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # DNS is NOT patched because security check runs before DNS.
    app = create_app(_settings(allowed_domains="example.com"))
    with TestClient(app, raise_server_exceptions=False) as tc:
        resp = tc.post(
            "/api/web-fetch/v1/fetch",
            headers=_auth_headers(),
            json={"url": "https://attacker.com/steal"},
        )
    assert resp.status_code == 403
    body = resp.json()
    assert body["code"] == "FETCH.DOMAIN_NOT_ALLOWED"


# ---------------------------------------------------------------------------
# Private IP → 403
# ---------------------------------------------------------------------------

def test_private_ip_returns_403(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # DNS resolves to a private address.
    monkeypatch.setattr(
        "socket.getaddrinfo",
        lambda host, port, *a, **kw: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.100", 80))
        ],
    )
    app = create_app(_settings(allowed_domains="*"))
    with TestClient(app, raise_server_exceptions=False) as tc:
        resp = tc.post(
            "/api/web-fetch/v1/fetch",
            headers=_auth_headers(),
            json={"url": "https://internal.corp/secret"},
        )
    assert resp.status_code == 403
    body = resp.json()
    assert body["code"] == "FETCH.PRIVATE_ADDRESS"


def test_aws_metadata_ip_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "socket.getaddrinfo",
        lambda host, port, *a, **kw: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 80))
        ],
    )
    app = create_app(_settings(allowed_domains="*"))
    with TestClient(app, raise_server_exceptions=False) as tc:
        resp = tc.post(
            "/api/web-fetch/v1/fetch",
            headers=_auth_headers(),
            json={"url": "https://169.254.169.254/latest/meta-data/"},
        )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Invalid scheme → 400
# ---------------------------------------------------------------------------

def test_file_scheme_returns_400(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = create_app(_settings(allowed_domains="*"))
    with TestClient(app, raise_server_exceptions=False) as tc:
        resp = tc.post(
            "/api/web-fetch/v1/fetch",
            headers=_auth_headers(),
            json={"url": "file:///etc/passwd"},
        )
    assert resp.status_code == 400
    assert resp.json()["code"] == "FETCH.INVALID_SCHEME"


# ---------------------------------------------------------------------------
# HEAD method
# ---------------------------------------------------------------------------

def test_head_method_allowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_dns_public(monkeypatch)
    _patch_client(
        [_make_response(body=b"")],
        monkeypatch,
    )
    app = create_app(_settings())
    with TestClient(app, raise_server_exceptions=False) as tc:
        resp = tc.post(
            "/api/web-fetch/v1/fetch",
            headers=_auth_headers(),
            json={"url": "https://example.com/", "method": "HEAD"},
        )
    assert resp.status_code == 200


def test_post_method_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_dns_public(monkeypatch)
    app = create_app(_settings())
    with TestClient(app, raise_server_exceptions=False) as tc:
        resp = tc.post(
            "/api/web-fetch/v1/fetch",
            headers=_auth_headers(),
            json={"url": "https://example.com/", "method": "POST"},
        )
    assert resp.status_code == 502  # FetchUpstreamError


# ---------------------------------------------------------------------------
# application/json content-type passes restriction filter
# ---------------------------------------------------------------------------

def test_application_json_passes_restriction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_dns_public(monkeypatch)
    _patch_client(
        [_make_response(content_type="application/json", body=b'{"ok": true}')],
        monkeypatch,
    )
    app = create_app(_settings(restrict_content_types=True))
    with TestClient(app, raise_server_exceptions=False) as tc:
        resp = tc.post(
            "/api/web-fetch/v1/fetch",
            headers=_auth_headers(),
            json={"url": "https://api.example.com/data"},
        )
    assert resp.status_code == 200
    assert resp.json()["body"] == '{"ok": true}'
