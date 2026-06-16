"""Conformance test fixtures.

Provides authenticated HTTP clients for two separate tenants to verify
cross-tenant isolation. Uses the BFF as the single entry point.
"""

import os
from dataclasses import dataclass
from typing import Optional

import httpx
import pytest


BFF_URL = os.getenv("BFF_URL", "http://localhost:8005")
ORCHESTRATOR_URL = os.getenv("ORCHESTRATOR_URL", "http://localhost:8006")
SCHEDULER_URL = os.getenv("SCHEDULER_URL", "http://localhost:8007")


@dataclass
class TenantClient:
    """Authenticated client for a specific tenant user."""

    client: httpx.Client
    tenant_id: str
    user_id: str
    email: str


def _login(email: str, password: str) -> tuple[httpx.Client, str, str, str]:
    """Login and return (client_with_cookies, tenant_id, user_id, email)."""
    client = httpx.Client(base_url=BFF_URL, timeout=30)
    r = client.post(
        "/api/bff/v1/auth/login",
        json={"email": email, "password": password},
    )
    assert r.status_code == 200, f"Login failed for {email}: {r.text}"
    data = r.json()
    return client, data["tenant_id"], data["user_id"], data["email"]


@pytest.fixture(scope="session")
def alice() -> TenantClient:
    """Tenant A user — alice@tenant-a.local (foundation realm seed)."""
    client, tenant_id, user_id, email = _login(
        "alice@tenant-a.local", "Tenant-A-Dev-Pwd-15chars!"
    )
    return TenantClient(client=client, tenant_id=tenant_id, user_id=user_id, email=email)


@pytest.fixture(scope="session")
def bob() -> TenantClient:
    """The "other tenant" user for cross-tenant isolation — carol@tenant-b.local (Tenant B).

    The fixture name is kept as ``bob`` to avoid churning all reference sites;
    it logs in as carol@tenant-b.local, who lives in a *different* tenant than
    alice (Tenant A), which is what the isolation tests require.
    """
    client, tenant_id, user_id, email = _login(
        "carol@tenant-b.local", "Tenant-B-Dev-Pwd-15chars!"
    )
    return TenantClient(client=client, tenant_id=tenant_id, user_id=user_id, email=email)


@pytest.fixture(scope="session")
def unauthenticated_client() -> httpx.Client:
    """Client with no auth credentials."""
    return httpx.Client(base_url=BFF_URL, timeout=30)


@pytest.fixture(scope="session")
def orchestrator_client() -> httpx.Client:
    """Direct session-orchestrator client (for health checks)."""
    return httpx.Client(base_url=ORCHESTRATOR_URL, timeout=10)


@pytest.fixture(scope="session")
def scheduler_client() -> httpx.Client:
    """Direct agent-scheduler client (for health checks)."""
    return httpx.Client(base_url=SCHEDULER_URL, timeout=10)
