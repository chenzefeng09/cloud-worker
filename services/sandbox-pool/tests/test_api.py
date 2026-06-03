"""HTTP-layer smoke tests for sandbox-pool.

We patch ``PoolDockerRuntime`` so no Docker daemon is needed during tests.
"""

from __future__ import annotations

import itertools
from collections.abc import Iterator
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from sandbox_pool.config import Settings
from sandbox_pool.main import create_app


@pytest.fixture
def settings() -> Settings:
    return Settings(
        env="test",
        log_level="WARNING",
        idle_low_watermark=0,  # disable background spawning for stable tests
        idle_high_watermark=5,
        max_total=10,
        warmup_templates=["base"],
        otel_enabled=False,
        sandbox_image="test-image:latest",
        docker_network="test_net",
    )


def _mock_runtime_factory() -> MagicMock:
    runtime = MagicMock()
    _counter = itertools.count()
    runtime.start_idle_container.side_effect = (
        lambda template: f"{next(_counter):012x}"
    )
    return runtime


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    mock_runtime = _mock_runtime_factory()
    with patch(
        "sandbox_pool.pool.PoolDockerRuntime",
        return_value=mock_runtime,
    ):
        app = create_app(settings)
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def test_healthz(client: TestClient) -> None:
    resp = client.get("/api/sandbox-pool/v1/healthz")
    assert resp.status_code == 200
    assert resp.json()["service"] == "sandbox-pool"


def test_readyz_returns_pool_snapshot(client: TestClient) -> None:
    resp = client.get("/api/sandbox-pool/v1/readyz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "OK"
    assert "pool" in body


def test_acquire_requires_tenant_header(client: TestClient) -> None:
    resp = client.post(
        "/api/sandbox-pool/v1/sandboxes/acquire",
        json={"template": "base"},
    )
    assert resp.status_code == 401


def test_acquire_and_release_round_trip(client: TestClient) -> None:
    headers = {"X-Tenant-Id": "tnt_01J5TEST00000000000000TNT1"}
    acq = client.post(
        "/api/sandbox-pool/v1/sandboxes/acquire",
        headers=headers,
        json={"template": "base"},
    )
    assert acq.status_code == 201, acq.text
    sandbox_id = acq.json()["sandbox_id"]
    # Real short container id: 12 hex chars (not the old "sb_..." fake UUID)
    assert len(sandbox_id) == 12
    assert all(c in "0123456789abcdef" for c in sandbox_id)

    snap = client.get("/api/sandbox-pool/v1/sandboxes/_pool")
    assert snap.json()["total"] == 1

    rel = client.delete(
        f"/api/sandbox-pool/v1/sandboxes/{sandbox_id}",
        headers=headers,
    )
    assert rel.status_code == 204

    snap = client.get("/api/sandbox-pool/v1/sandboxes/_pool")
    assert snap.json()["total"] == 0
