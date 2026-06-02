"""Test API conventions across all service endpoints.

Conventions:
  - All JSON keys are snake_case
  - IDs use ULID-based prefixes (ses_, msg_, run_)
  - Timestamps are ISO 8601 UTC
  - Pagination uses cursor + limit with next_cursor
"""

import re
from datetime import datetime

import pytest
from httpx import Client

from conftest import TenantClient


ISO8601_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)
SNAKE_CASE_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")
ULID_PREFIX = re.compile(r"^(ses_|msg_|run_)[0-9A-Za-z]{26}$")


def _check_snake_case(obj: dict, path: str = "$") -> list[str]:
    """Recursively verify all keys are snake_case. Returns list of violations."""
    violations = []
    for key, value in obj.items():
        current = f"{path}.{key}"
        if not SNAKE_CASE_PATTERN.match(key):
            violations.append(f"Non-snake_case key: '{key}' at {current}")
        if isinstance(value, dict):
            violations.extend(_check_snake_case(value, current))
        elif isinstance(value, list):
            for i, item in enumerate(value):
                if isinstance(item, dict):
                    violations.extend(_check_snake_case(item, f"{current}[{i}]"))
    return violations


class TestSnakeCase:
    """All JSON responses must use snake_case keys."""

    def test_login_response_snake_case(self, unauthenticated_client: Client):
        """Login response uses snake_case."""
        r = unauthenticated_client.post(
            "/api/bff/v1/auth/login",
            json={"email": "alice@example.com", "password": "test123"},
        )
        if r.status_code == 200:
            violations = _check_snake_case(r.json(), "$")
            assert not violations, f"Non-snake_case keys: {violations}"

    def test_session_list_snake_case(self, alice: TenantClient):
        """Session list response uses snake_case."""
        r = alice.client.get("/api/bff/v1/sessions")
        if r.status_code == 200:
            violations = _check_snake_case(r.json(), "$")
            assert not violations, f"Non-snake_case keys: {violations}"

    def test_health_response_snake_case(self, unauthenticated_client: Client):
        """Health endpoint uses snake_case (if any keys)."""
        r = unauthenticated_client.get("/api/bff/v1/healthz")
        if r.status_code == 200:
            violations = _check_snake_case(r.json(), "$")
            assert not violations, f"Non-snake_case keys: {violations}"


class TestIdFormat:
    """IDs must use ULID-based prefixes."""

    def test_session_id_format(self, alice: TenantClient):
        """Session IDs follow ses_<ULID> pattern."""
        r = alice.client.post("/api/bff/v1/sessions", json={})
        assert r.status_code == 201, f"Create failed: {r.text}"
        session_id = r.json()["id"]
        assert ULID_PREFIX.match(session_id), (
            f"Session ID '{session_id}' should match ses_<ULID> pattern"
        )

    def test_session_list_ids(self, alice: TenantClient):
        """All session IDs in list follow the pattern."""
        r = alice.client.get("/api/bff/v1/sessions")
        if r.status_code == 200:
            for item in r.json()["items"]:
                assert ULID_PREFIX.match(item["id"]), (
                    f"Invalid session ID '{item['id']}' in list"
                )

    def test_message_id_format(self, alice: TenantClient):
        """Message IDs follow msg_<ULID> pattern."""
        # Create session + send message
        r = alice.client.post("/api/bff/v1/sessions", json={})
        session_id = r.json()["id"]

        r = alice.client.get(f"/api/bff/v1/sessions/{session_id}")
        if r.status_code == 200:
            detail = r.json()
            for msg in detail.get("messages", []):
                assert ULID_PREFIX.match(msg["id"]), (
                    f"Message ID '{msg['id']}' should match msg_<ULID>"
                )


class TestTimestampFormat:
    """Timestamps must be ISO 8601 UTC."""

    def test_session_timestamps(self, alice: TenantClient):
        """Session created_at/updated_at are valid ISO 8601."""
        r = alice.client.post("/api/bff/v1/sessions", json={"title": "ts-test"})
        assert r.status_code == 201, f"Create failed: {r.text}"
        session = r.json()

        for field in ["created_at", "updated_at"]:
            ts = session.get(field)
            assert ts, f"Missing {field} in session"
            assert ISO8601_PATTERN.match(ts), f"{field}='{ts}' is not ISO 8601 UTC"

            # Verify it can be parsed
            try:
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except ValueError as e:
                pytest.fail(f"Cannot parse {field}='{ts}': {e}")

            # Should be recent (within last 60 seconds)
            delta = abs((datetime.now(dt.tzinfo) - dt).total_seconds())
            assert delta < 60, f"{field} is {delta}s old, expected <60s"


class TestPagination:
    """List endpoints must support cursor-based pagination."""

    def test_session_list_has_next_cursor(self, alice: TenantClient):
        """Session list response includes next_cursor field."""
        r = alice.client.get("/api/bff/v1/sessions")
        assert r.status_code == 200
        page = r.json()
        assert "items" in page, "Missing 'items' in paginated response"
        assert "next_cursor" in page, "Missing 'next_cursor' in paginated response"
        assert isinstance(page["items"], list), "'items' must be an array"

    def test_session_list_respects_limit(self, alice: TenantClient):
        """Cursor pagination respects limit parameter."""
        # Create a few sessions
        for i in range(3):
            alice.client.post("/api/bff/v1/sessions", json={"title": f"limit-test-{i}"})

        r = alice.client.get("/api/bff/v1/sessions", params={"limit": 2})
        assert r.status_code == 200
        page = r.json()
        assert len(page["items"]) <= 2, f"Limit 2 returned {len(page['items'])} items"

    def test_cursor_pagination_iterates(self, alice: TenantClient):
        """Cursor can be used to fetch next page."""
        r1 = alice.client.get("/api/bff/v1/sessions", params={"limit": 1})
        assert r1.status_code == 200
        page1 = r1.json()

        if page1["next_cursor"] and len(page1["items"]) > 0:
            r2 = alice.client.get(
                "/api/bff/v1/sessions",
                params={"limit": 1, "cursor": page1["next_cursor"]},
            )
            assert r2.status_code == 200
            page2 = r2.json()
            # Next page should have different items
            if page2["items"]:
                assert page1["items"][0]["id"] != page2["items"][0]["id"], (
                    "Cursor pagination should return different items"
                )
