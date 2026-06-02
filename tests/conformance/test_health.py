"""Test health check endpoints on all services.

Required shape:
  - status: "OK" | "DEGRADED" | "DOWN"
  - service: service name
  - version: version string
"""

import re

import pytest
from httpx import Client

from conftest import BFF_URL, ORCHESTRATOR_URL, SCHEDULER_URL


SEMVER_PATTERN = re.compile(r"^\d+\.\d+\.\d+")

HEALTH_ENDPOINTS = [
    ("BFF", f"{BFF_URL}/api/bff/v1/healthz", f"{BFF_URL}/api/bff/v1/readyz"),
    ("Session Orchestrator", f"{ORCHESTRATOR_URL}/api/session/v1/healthz", None),
    ("Agent Scheduler", f"{SCHEDULER_URL}/api/scheduler/v1/healthz", None),
]


def _assert_health_shape(body: dict, service_name: str):
    """Assert health response has required fields."""
    assert isinstance(body, dict), f"Health response should be object, got {type(body)}"
    assert "status" in body, f"Missing 'status' in {service_name} health"
    assert body["status"] in ("OK", "DEGRADED", "DOWN"), (
        f"Invalid status '{body['status']}' in {service_name}"
    )
    assert "service" in body, f"Missing 'service' in {service_name} health"
    assert "version" in body, f"Missing 'version' in {service_name} health"
    assert SEMVER_PATTERN.match(body["version"]), (
        f"Version '{body['version']}' not semver in {service_name}"
    )


class TestHealthEndpoints:
    """All services must expose /healthz returning the standard shape."""

    @pytest.mark.parametrize("name,healthz_url,readyz_url", HEALTH_ENDPOINTS)
    def test_healthz_ok(self, name: str, healthz_url: str, readyz_url: str | None):
        """Each service's /healthz returns 200 with correct shape."""
        client = Client(timeout=10)
        try:
            r = client.get(healthz_url)
            assert r.status_code == 200, f"{name} healthz returned {r.status_code}: {r.text}"
            _assert_health_shape(r.json(), name)
        finally:
            client.close()

    @pytest.mark.parametrize("name,healthz_url,readyz_url", HEALTH_ENDPOINTS)
    def test_readyz_ok(self, name: str, healthz_url: str, readyz_url: str | None):
        """Each service's /readyz returns 200 (where implemented)."""
        if readyz_url is None:
            pytest.skip(f"{name} does not expose /readyz")
        client = Client(timeout=10)
        try:
            r = client.get(readyz_url)
            assert r.status_code == 200, f"{name} readyz returned {r.status_code}: {r.text}"
            _assert_health_shape(r.json(), name)
        finally:
            client.close()

    def test_bff_healthz_no_auth(self):
        """BFF /healthz must be accessible without authentication."""
        client = Client(timeout=10)
        try:
            r = client.get(f"{BFF_URL}/api/bff/v1/healthz")
            assert r.status_code == 200, f"healthz should not require auth: {r.status_code}"
        finally:
            client.close()

    def test_orchestrator_healthz_no_auth(self):
        """Orchestrator /healthz must be accessible without tenant headers."""
        client = Client(timeout=10)
        try:
            r = client.get(f"{ORCHESTRATOR_URL}/api/session/v1/healthz")
            assert r.status_code == 200, f"healthz should not require auth: {r.status_code}"
        finally:
            client.close()

    def test_scheduler_healthz_no_auth(self):
        """Scheduler /healthz must be accessible without tenant headers."""
        client = Client(timeout=10)
        try:
            r = client.get(f"{SCHEDULER_URL}/api/scheduler/v1/healthz")
            assert r.status_code == 200, f"healthz should not require auth: {r.status_code}"
        finally:
            client.close()
