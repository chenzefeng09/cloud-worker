"""Tests for BFF GET /fleet/topology endpoint."""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SANDBOX_ITEMS = [
    {
        "container_id": "ctr_aaa",
        "name": "worker-alpha",
        "status": "running",
        "tenant_id": "tnt_test",
        "session_id": "ses_111",
        "run_id": "run_001",
        "created_at": "2026-06-04T00:00:00Z",
    },
    {
        "container_id": "ctr_bbb",
        "name": "worker-beta",
        "status": "idle",
        "tenant_id": "tnt_test",
        "session_id": None,
        "run_id": None,
        "created_at": "2026-06-04T01:00:00Z",
    },
]

MCP_ITEMS = [
    {
        "id": "mcp_github",
        "name": "github",
        "transport": "http",
        "endpoint": "https://mcp.github.example",
        "command": None,
        "env": {},
        "scopes": [],
        "enabled": True,
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
    },
    {
        "id": "mcp_linear",
        "name": "linear",
        "transport": "http",
        "endpoint": "https://mcp.linear.example",
        "command": None,
        "env": {},
        "scopes": [],
        "enabled": False,
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
    },
]


class FakeFoundationTopology:
    """Stub that returns deterministic sandbox + mcp responses."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.sandbox_response: tuple[int, dict] = (200, {"items": SANDBOX_ITEMS})
        self.mcp_response: tuple[int, dict] = (200, {"items": MCP_ITEMS})

    def route(self, method: str, url: str, **kwargs) -> httpx.Response:
        self.calls.append((method, url))
        if "/sandboxes" in url and "scheduler" in url:
            status, body = self.sandbox_response
        elif "/mcp-servers" in url:
            status, body = self.mcp_response
        elif "/sessions/me" in url:
            return httpx.Response(200, json={
                "user_id": "usr_01J5TEST0000000000000USER1",
                "tenant_id": "tnt_01J5TEST00000000000000TNT1",
                "email": "test@example.com",
                "display_name": "Test User",
                "roles": [],
                "mfa_enabled": False,
                "locale": "zh-CN",
            })
        else:
            status, body = 200, {"ok": True}
        return httpx.Response(status, json=body)


@pytest.fixture
def fake_topo(monkeypatch: pytest.MonkeyPatch) -> FakeFoundationTopology:
    fake = FakeFoundationTopology()

    class _AsyncClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self) -> "_AsyncClient":
            return self

        async def __aexit__(self, *args) -> None:
            return None

        async def get(self, url: str, **kw) -> httpx.Response:
            return fake.route("GET", url, **kw)

        async def post(self, url: str, **kw) -> httpx.Response:
            return fake.route("POST", url, **kw)

        async def delete(self, url: str, **kw) -> httpx.Response:
            return fake.route("DELETE", url, **kw)

        async def patch(self, url: str, **kw) -> httpx.Response:
            return fake.route("PATCH", url, **kw)

    monkeypatch.setattr(httpx, "AsyncClient", _AsyncClient)
    return fake


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_topology_requires_auth(client: TestClient) -> None:
    """Unauthenticated request must return 401."""
    resp = client.get("/api/bff/v1/fleet/topology")
    assert resp.status_code == 401


def test_topology_returns_nodes_and_edges(
    client: TestClient,
    authed_cookie: dict[str, str],
    fake_topo: FakeFoundationTopology,
) -> None:
    """Happy path: nodes and edges are assembled from upstream data."""
    resp = client.get("/api/bff/v1/fleet/topology", cookies=authed_cookie)
    assert resp.status_code == 200, resp.text

    data = resp.json()
    assert "nodes" in data
    assert "edges" in data
    assert isinstance(data["nodes"], list)
    assert isinstance(data["edges"], list)


def test_topology_worker_nodes_from_sandboxes(
    client: TestClient,
    authed_cookie: dict[str, str],
    fake_topo: FakeFoundationTopology,
) -> None:
    """Worker nodes are derived from sandbox containers."""
    resp = client.get("/api/bff/v1/fleet/topology", cookies=authed_cookie)
    data = resp.json()

    workers = [n for n in data["nodes"] if n["type"] == "worker"]
    worker_ids = {w["id"] for w in workers}

    assert "ctr_aaa" in worker_ids, "sandbox ctr_aaa should produce a worker node"
    assert "ctr_bbb" in worker_ids, "sandbox ctr_bbb should produce a worker node"
    assert len(workers) == 2


def test_topology_task_nodes_from_session_ids(
    client: TestClient,
    authed_cookie: dict[str, str],
    fake_topo: FakeFoundationTopology,
) -> None:
    """Task nodes are created for sandboxes that carry a session_id."""
    resp = client.get("/api/bff/v1/fleet/topology", cookies=authed_cookie)
    data = resp.json()

    tasks = [n for n in data["nodes"] if n["type"] == "task"]
    # ctr_aaa has session_id=ses_111; ctr_bbb has no session_id → 1 task
    assert len(tasks) == 1
    assert tasks[0]["id"] == "task:ses_111"


def test_topology_mcp_nodes(
    client: TestClient,
    authed_cookie: dict[str, str],
    fake_topo: FakeFoundationTopology,
) -> None:
    """MCP nodes are created for all MCP servers (enabled and disabled)."""
    resp = client.get("/api/bff/v1/fleet/topology", cookies=authed_cookie)
    data = resp.json()

    mcps = [n for n in data["nodes"] if n["type"] == "mcp"]
    mcp_ids = {m["id"] for m in mcps}

    assert "mcp:mcp_github" in mcp_ids
    assert "mcp:mcp_linear" in mcp_ids
    assert len(mcps) == 2

    # Check status flags
    github_node = next(m for m in mcps if m["id"] == "mcp:mcp_github")
    linear_node = next(m for m in mcps if m["id"] == "mcp:mcp_linear")
    assert github_node["status"] == "enabled"
    assert linear_node["status"] == "disabled"


def test_topology_worker_task_edge(
    client: TestClient,
    authed_cookie: dict[str, str],
    fake_topo: FakeFoundationTopology,
) -> None:
    """worker→task edges are created for sandboxes with session_id."""
    resp = client.get("/api/bff/v1/fleet/topology", cookies=authed_cookie)
    data = resp.json()

    wt_edges = [e for e in data["edges"] if e["kind"] == "worker→task"]
    assert len(wt_edges) == 1
    edge = wt_edges[0]
    assert edge["from"] == "ctr_aaa"
    assert edge["to"] == "task:ses_111"


def test_topology_worker_mcp_edges_only_enabled(
    client: TestClient,
    authed_cookie: dict[str, str],
    fake_topo: FakeFoundationTopology,
) -> None:
    """worker→mcp edges are only created for ENABLED MCPs (approximate)."""
    resp = client.get("/api/bff/v1/fleet/topology", cookies=authed_cookie)
    data = resp.json()

    wm_edges = [e for e in data["edges"] if e["kind"] == "worker→mcp"]

    # Only github is enabled → 2 workers × 1 enabled mcp = 2 edges
    assert len(wm_edges) == 2
    targets = {e["to"] for e in wm_edges}
    assert targets == {"mcp:mcp_github"}
    # disabled linear must NOT appear
    disabled_targets = {e["to"] for e in wm_edges if "linear" in e["to"]}
    assert not disabled_targets


def test_topology_nodes_have_no_xy(
    client: TestClient,
    authed_cookie: dict[str, str],
    fake_topo: FakeFoundationTopology,
) -> None:
    """Nodes returned by the backend must NOT contain x or y coordinates."""
    resp = client.get("/api/bff/v1/fleet/topology", cookies=authed_cookie)
    data = resp.json()

    for node in data["nodes"]:
        assert "x" not in node, f"node {node['id']} should not have x coordinate"
        assert "y" not in node, f"node {node['id']} should not have y coordinate"


def test_topology_empty_when_no_sandboxes(
    client: TestClient,
    authed_cookie: dict[str, str],
    fake_topo: FakeFoundationTopology,
) -> None:
    """With no sandboxes, worker + task nodes and their edges are absent."""
    fake_topo.sandbox_response = (200, {"items": []})

    resp = client.get("/api/bff/v1/fleet/topology", cookies=authed_cookie)
    assert resp.status_code == 200

    data = resp.json()
    workers = [n for n in data["nodes"] if n["type"] == "worker"]
    tasks   = [n for n in data["nodes"] if n["type"] == "task"]
    assert workers == []
    assert tasks   == []
    assert data["edges"] == []


def test_topology_handles_upstream_failure_gracefully(
    client: TestClient,
    authed_cookie: dict[str, str],
    fake_topo: FakeFoundationTopology,
) -> None:
    """If sandbox upstream returns 500, topology still returns 200 with what it has."""
    fake_topo.sandbox_response = (500, {"detail": "scheduler down"})

    resp = client.get("/api/bff/v1/fleet/topology", cookies=authed_cookie)
    assert resp.status_code == 200
    data = resp.json()
    # No worker/task nodes since sandbox call failed
    workers = [n for n in data["nodes"] if n["type"] == "worker"]
    assert workers == []
