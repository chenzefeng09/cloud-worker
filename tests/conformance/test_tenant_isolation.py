"""Test tenant isolation: Tenant A resources must be invisible to Tenant B.

Covers:
  - Tenant A creates session → Tenant B GET returns 404
  - Tenant A creates session → Tenant A GET returns 200
  - Cross-tenant access via direct session ID
"""

import pytest
from httpx import Client

from conftest import TenantClient


@pytest.mark.integration
class TestTenantIsolation:
    """Verify that sessions are strictly isolated by tenant_id."""

    def test_own_session_visible(self, alice: TenantClient):
        """Tenant A creates a session and can retrieve it."""
        r = alice.client.post(
            "/api/bff/v1/sessions",
            json={"title": "alice-test-session"},
        )
        assert r.status_code == 201, f"Create failed: {r.text}"
        session = r.json()
        session_id = session["id"]

        r = alice.client.get(f"/api/bff/v1/sessions/{session_id}")
        assert r.status_code == 200
        assert r.json()["id"] == session_id

    def test_cross_tenant_session_invisible(self, alice: TenantClient, bob: TenantClient):
        """Tenant B cannot access Tenant A's session — returns 404."""
        # Alice creates a session
        r = alice.client.post(
            "/api/bff/v1/sessions",
            json={"title": "alice-private-session"},
        )
        assert r.status_code == 201, f"Create failed: {r.text}"
        alice_session_id = r.json()["id"]

        # Bob tries to access it
        r = bob.client.get(f"/api/bff/v1/sessions/{alice_session_id}")
        assert r.status_code == 404, (
            f"Cross-tenant access should return 404, got {r.status_code}: {r.text}"
        )

    def test_cross_tenant_not_in_list(self, alice: TenantClient, bob: TenantClient):
        """Tenant B's session list must not include Tenant A's sessions."""
        # Alice creates a session
        alice.client.post(
            "/api/bff/v1/sessions",
            json={"title": "alice-list-test"},
        )

        # Bob lists his sessions
        r = bob.client.get("/api/bff/v1/sessions")
        assert r.status_code == 200
        page = r.json()
        for item in page["items"]:
            assert item["title"] != "alice-list-test", (
                f"Tenant B should not see Tenant A's sessions: {item}"
            )

    def test_update_cross_tenant_rejected(self, alice: TenantClient, bob: TenantClient):
        """Tenant B cannot update Tenant A's session — returns 404."""
        r = alice.client.post("/api/bff/v1/sessions", json={"title": "alice-update-test"})
        session_id = r.json()["id"]

        r = bob.client.patch(
            f"/api/bff/v1/sessions/{session_id}",
            json={"title": "hijacked"},
        )
        assert r.status_code == 404, (
            f"Cross-tenant update should return 404, got {r.status_code}"
        )

    def test_delete_cross_tenant_rejected(self, alice: TenantClient, bob: TenantClient):
        """Tenant B cannot delete Tenant A's session — returns 404."""
        r = alice.client.post("/api/bff/v1/sessions", json={"title": "alice-delete-test"})
        session_id = r.json()["id"]

        r = bob.client.delete(f"/api/bff/v1/sessions/{session_id}")
        assert r.status_code == 404, (
            f"Cross-tenant delete should return 404, got {r.status_code}"
        )

    def test_session_list_only_own_tenant(self, alice: TenantClient):
        """Listing sessions returns only the current tenant's sessions."""
        r = alice.client.get("/api/bff/v1/sessions")
        assert r.status_code == 200
        page = r.json()
        assert "items" in page
        for item in page["items"]:
            assert item["tenant_id"] == alice.tenant_id, (
                f"Session {item['id']} belongs to {item['tenant_id']}, "
                f"not {alice.tenant_id}"
            )
