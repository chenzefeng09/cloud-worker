"""Test that all error responses follow RFC 9457 Problem Details.

Required shape:
  - type: URI reference
  - title: human-readable summary
  - status: HTTP status code (int)
  - detail: explanation
  - instance: request path
  - code: FOUNDATION.<SERVICE>.<CATEGORY>.<NAME>
"""

import re

import pytest
from httpx import Client

from conftest import TenantClient, BFF_URL


ERROR_CODE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*(?:\.[A-Z][A-Z0-9_]*)*$")


def _assert_rfc_9457_shape(body: dict, expected_status: int, path_hint: str = ""):
    """Assert that body conforms to RFC 9457 Problem Details."""
    assert isinstance(body, dict), f"Error body should be object, got {type(body)}"

    # Required fields per RFC 9457
    assert "type" in body, f"Missing 'type' field in error: {body}"
    assert "title" in body, f"Missing 'title' field in error: {body}"
    assert "status" in body, f"Missing 'status' field in error: {body}"
    assert "detail" in body, f"Missing 'detail' field in error: {body}"

    # Status must be an int matching HTTP status
    assert isinstance(body["status"], int), f"'status' must be int, got {type(body['status'])}"
    assert body["status"] == expected_status, (
        f"status {body['status']} != HTTP {expected_status}"
    )

    # Custom extension: error code
    assert "code" in body, f"Missing 'code' field in error: {body}"
    assert ERROR_CODE_PATTERN.match(body["code"]), (
        f"Error code '{body['code']}' should match DOT.NOTATION pattern"
    )

    # instance should reference the request path
    if "instance" in body and path_hint:
        assert path_hint in body["instance"], (
            f"instance '{body['instance']}' should contain path '{path_hint}'"
        )


class TestErrorFormat:
    """All error responses must follow RFC 9457."""

    def test_401_unauthorized(self, unauthenticated_client: Client):
        """Unauthenticated requests get RFC 9457 401 error."""
        r = unauthenticated_client.get("/api/bff/v1/sessions")
        assert r.status_code == 401, f"Expected 401, got {r.status_code}"
        _assert_rfc_9457_shape(r.json(), 401, path_hint="/api/bff/v1/sessions")

    def test_404_not_found(self, alice: TenantClient):
        """Non-existent session returns RFC 9457 404."""
        r = alice.client.get("/api/bff/v1/sessions/ses_nonexistent123")
        assert r.status_code == 404, f"Expected 404, got {r.status_code}"
        _assert_rfc_9457_shape(r.json(), 404, path_hint="ses_nonexistent123")

    def test_422_validation_error(self, alice: TenantClient):
        """Invalid request body returns RFC 9457 422."""
        r = alice.client.post(
            "/api/bff/v1/sessions",
            json={"title": "x" * 500},  # exceeds maxLength 200
        )
        assert r.status_code == 422, f"Expected 422, got {r.status_code}: {r.text}"
        _assert_rfc_9457_shape(r.json(), 422, path_hint="/api/bff/v1/sessions")

    def test_405_method_not_allowed(self, alice: TenantClient):
        """Wrong HTTP method returns RFC 9457 error."""
        r = alice.client.put("/api/bff/v1/sessions")
        # May be 405 or 404 depending on routing
        assert r.status_code in (404, 405), f"Unexpected status: {r.status_code}"
        _assert_rfc_9457_shape(r.json(), r.status_code)

    def test_401_invalid_credentials(self, unauthenticated_client: Client):
        """Login with wrong password returns RFC 9457 error."""
        r = unauthenticated_client.post(
            "/api/bff/v1/auth/login",
            json={"email": "alice@example.com", "password": "wrongpassword"},
        )
        assert r.status_code in (401, 403), f"Expected 401/403, got {r.status_code}"
        _assert_rfc_9457_shape(r.json(), r.status_code, path_hint="login")
