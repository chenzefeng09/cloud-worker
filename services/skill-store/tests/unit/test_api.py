"""HTTP-layer tests for skill-store."""

from __future__ import annotations

import hashlib
import json

from fastapi.testclient import TestClient


def _create_skill(client: TestClient, headers: dict[str, str], name: str = "poet") -> dict:
    resp = client.post(
        "/api/skills/v1/skills",
        headers=headers,
        json={"name": name, "description": "demo", "visibility": "private"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_healthz(app_client: TestClient) -> None:
    resp = app_client.get("/api/skills/v1/healthz")
    assert resp.status_code == 200
    assert resp.json()["service"] == "skill-store"


def test_create_requires_tenant(app_client: TestClient) -> None:
    # authz guard fires before the current_tenant dependency: missing tenant
    # is caught at the guard level (403) before the 401 dependency runs.
    resp = app_client.post("/api/skills/v1/skills", json={"name": "p"})
    assert resp.status_code in (401, 403)


def test_create_and_list_skill(app_client: TestClient, tenant_headers: dict[str, str]) -> None:
    created = _create_skill(app_client, tenant_headers)
    listing = app_client.get("/api/skills/v1/skills", headers=tenant_headers).json()
    assert listing["total"] == 1
    assert listing["items"][0]["id"] == created["id"]


def test_duplicate_skill_conflict(
    app_client: TestClient, tenant_headers: dict[str, str],
) -> None:
    _create_skill(app_client, tenant_headers)
    resp = app_client.post(
        "/api/skills/v1/skills",
        headers=tenant_headers,
        json={"name": "poet", "visibility": "private"},
    )
    assert resp.status_code == 409


def test_upload_version(app_client: TestClient, tenant_headers: dict[str, str]) -> None:
    skill = _create_skill(app_client, tenant_headers)
    tarball = b"fake-tarball-payload"
    expected = hashlib.sha256(tarball).hexdigest()
    resp = app_client.post(
        f"/api/skills/v1/skills/{skill['id']}/versions",
        headers=tenant_headers,
        data={
            "version": "1.0.0",
            "manifest": json.dumps({"entry": "main.py"}),
            "expected_sha256": expected,
        },
        files={"file": ("skill.tar.gz", tarball, "application/gzip")},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["sha256"] == expected
    assert body["manifest"] == {"entry": "main.py"}


def test_upload_version_sha_mismatch(
    app_client: TestClient, tenant_headers: dict[str, str],
) -> None:
    skill = _create_skill(app_client, tenant_headers)
    resp = app_client.post(
        f"/api/skills/v1/skills/{skill['id']}/versions",
        headers=tenant_headers,
        data={"version": "1.0.0", "expected_sha256": "deadbeef"},
        files={"file": ("x.tar.gz", b"x", "application/gzip")},
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "SKILL.SHA256_MISMATCH"


def test_download_redirects_to_presigned(
    app_client: TestClient, tenant_headers: dict[str, str],
) -> None:
    skill = _create_skill(app_client, tenant_headers)
    app_client.post(
        f"/api/skills/v1/skills/{skill['id']}/versions",
        headers=tenant_headers,
        data={"version": "1.0.0"},
        files={"file": ("x.tar.gz", b"x", "application/gzip")},
    )
    resp = app_client.get(
        f"/api/skills/v1/skills/{skill['id']}/versions/1.0.0/download",
        headers=tenant_headers,
        follow_redirects=False,
    )
    assert resp.status_code == 307
    assert resp.headers["location"].startswith("memory://tenant/")


def test_delete_version(
    app_client: TestClient, tenant_headers: dict[str, str], blob_store,
) -> None:
    skill = _create_skill(app_client, tenant_headers)
    app_client.post(
        f"/api/skills/v1/skills/{skill['id']}/versions",
        headers=tenant_headers,
        data={"version": "1.0.0"},
        files={"file": ("x.tar.gz", b"x", "application/gzip")},
    )
    assert blob_store.size == 1
    resp = app_client.delete(
        f"/api/skills/v1/skills/{skill['id']}/versions/1.0.0",
        headers=tenant_headers,
    )
    assert resp.status_code == 204
    assert blob_store.size == 0


def test_get_skill_404(app_client: TestClient, tenant_headers: dict[str, str]) -> None:
    resp = app_client.get(
        "/api/skills/v1/skills/skl_unknown", headers=tenant_headers,
    )
    assert resp.status_code == 404


def test_tenant_isolation(
    app_client: TestClient,
    tenant_headers: dict[str, str],
    other_tenant_headers: dict[str, str],
) -> None:
    _create_skill(app_client, tenant_headers)
    other = app_client.get("/api/skills/v1/skills", headers=other_tenant_headers).json()
    assert other["total"] == 0


def test_internal_endpoint_requires_scope(app_client: TestClient) -> None:
    resp = app_client.get(
        "/api/skills/v1/internal/skills",
        params={"tenant_id": "tnt_01J5TEST00000000000000TNT1"},
    )
    assert resp.status_code == 403


def test_internal_endpoint_wrong_scope_rejected(app_client: TestClient) -> None:
    """A present but insufficient X-Service-Scopes value still produces 403."""
    resp = app_client.get(
        "/api/skills/v1/internal/skills",
        params={"tenant_id": "tnt_01J5TEST00000000000000TNT1"},
        headers={"X-Service-Scopes": "audit.write"},  # wrong scope
    )
    assert resp.status_code == 403


def test_internal_endpoint_multi_scope_header_accepted(
    app_client: TestClient,
    tenant_headers: dict[str, str],
) -> None:
    """X-Service-Scopes can carry multiple comma-separated scopes (as the
    scheduler injects FOUNDATION_SERVICE_SCOPES = 'mcp.read_internal,skill.read_internal').
    The required scope anywhere in the list must be accepted.
    """
    _create_skill(app_client, tenant_headers)
    resp = app_client.get(
        "/api/skills/v1/internal/skills",
        params={"tenant_id": "tnt_01J5TEST00000000000000TNT1"},
        headers={"X-Service-Scopes": "mcp.read_internal,skill.read_internal"},
    )
    assert resp.status_code == 200, resp.text


def test_internal_endpoint_returns_skills_with_versions(
    app_client: TestClient,
    tenant_headers: dict[str, str],
    internal_headers: dict[str, str],
) -> None:
    skill = _create_skill(app_client, tenant_headers)
    app_client.post(
        f"/api/skills/v1/skills/{skill['id']}/versions",
        headers=tenant_headers,
        data={"version": "1.0.0"},
        files={"file": ("x.tar.gz", b"x", "application/gzip")},
    )
    resp = app_client.get(
        "/api/skills/v1/internal/skills",
        params={"tenant_id": "tnt_01J5TEST00000000000000TNT1", "names": "poet"},
        headers=internal_headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["versions"][0]["version"] == "1.0.0"
