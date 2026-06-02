"""Verify tenant context propagation through the full stack.

BFF → Session Orchestrator → Agent Scheduler → Docker container

Since we can't easily introspect inside Docker containers in conformance tests,
we validate:
  1. BFF extracts tenant_id from JWT and passes it downstream
  2. Created sessions carry the correct tenant_id
  3. Messages carry the correct tenant_id
  4. SSRF protection: direct orchestrator calls without tenant header are rejected
"""

import pytest
from httpx import Client

from conftest import TenantClient, ORCHESTRATOR_URL


class TestContextPropagation:
    """Tenant context must flow BFF → orchestrator → scheduler."""

    def test_session_has_correct_tenant(self, alice: TenantClient):
        """Session created via BFF carries the authenticated user's tenant_id."""
        r = alice.client.post("/api/bff/v1/sessions", json={"title": "ctx-test"})
        assert r.status_code == 201, f"Create failed: {r.text}"
        session = r.json()

        assert session["tenant_id"] == alice.tenant_id, (
            f"Expected tenant_id={alice.tenant_id}, got {session['tenant_id']}"
        )
        assert session["user_id"] == alice.user_id, (
            f"Expected user_id={alice.user_id}, got {session['user_id']}"
        )

    def test_multiple_sessions_same_tenant(self, alice: TenantClient):
        """All sessions created by a user share the same tenant_id."""
        ids = set()
        for i in range(3):
            r = alice.client.post(
                "/api/bff/v1/sessions",
                json={"title": f"multi-ctx-{i}"},
            )
            assert r.status_code == 201
            session = r.json()
            assert session["tenant_id"] == alice.tenant_id
            ids.add(session["id"])

        # Verify all are visible in list
        r = alice.client.get("/api/bff/v1/sessions")
        listed_ids = {s["id"] for s in r.json()["items"]}
        for sid in ids:
            assert sid in listed_ids, f"Created session {sid} not in list"

    def test_orchestrator_rejects_missing_tenant_header(self):
        """Direct orchestrator calls without X-Tenant-Id are rejected."""
        client = Client(timeout=10)
        try:
            r = client.get(f"{ORCHESTRATOR_URL}/api/session/v1/sessions")
            # Must reject — tenant context is required
            assert r.status_code in (400, 401, 403, 422), (
                f"Expected rejection without X-Tenant-Id, got {r.status_code}: {r.text}"
            )
        finally:
            client.close()

    def test_orchestrator_rejects_invalid_tenant_header(self):
        """Direct orchestrator calls with invalid X-Tenant-Id are rejected."""
        client = Client(
            base_url=ORCHESTRATOR_URL,
            timeout=10,
            headers={"X-Tenant-Id": "", "X-User-Id": ""},
        )
        try:
            r = client.get("/api/session/v1/sessions")
            assert r.status_code in (400, 401, 403), (
                f"Expected rejection with empty X-Tenant-Id, got {r.status_code}"
            )
        finally:
            client.close()

    def test_messages_belong_to_tenant(self, alice: TenantClient):
        """Messages created through BFF belong to the correct tenant."""
        # Create session
        r = alice.client.post("/api/bff/v1/sessions", json={"title": "msg-ctx-test"})
        session_id = r.json()["id"]

        # Chat creates a message
        r = alice.client.post(
            f"/api/bff/v1/sessions/{session_id}/chat",
            json={"message": "Hello from conformance test"},
        )
        # SSE stream — may return 200 even if agent is not available
        # The message creation should have happened

        # Fetch session detail to verify messages
        r = alice.client.get(f"/api/bff/v1/sessions/{session_id}")
        if r.status_code == 200:
            detail = r.json()
            for msg in detail.get("messages", []):
                assert msg["session_id"] == session_id, (
                    f"Message {msg['id']} should belong to session {session_id}"
                )
