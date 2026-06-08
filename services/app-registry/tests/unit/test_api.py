"""End-to-end API tests against in-memory SQLite (authz mocked allow)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from app_registry.errors import AppForbiddenError
from app_registry.schemas.app import AppVersionCreate
from app_registry.services.app_service import AppService

PREFIX = "/api/apps/v1"


def _version_body() -> dict:
    return {
        "version": "1.0.0",
        "system_prompt": "You are a contract reviewer.",
        "user_prompt_template": "Review {{contract}} focusing on {{focus}}.",
        "model": "claude-opus-4-8",
        "reasoning_effort": "high",
        "sampling_params": {"temperature": 0.2},
        "forced_skills": ["pdf"],
        "forced_mcp_server_ids": [],
        "input_schema": [
            {"key": "contract", "type": "textarea", "label": "Contract", "required": True},
            {
                "key": "focus",
                "type": "select",
                "label": "Focus",
                "options": [{"value": "risk", "label": "Risk"}],
            },
        ],
    }


def _create_app(client: TestClient, headers: dict) -> dict:
    body = {
        "name": "Contract Reviewer", "description": "d", "icon": "C",
        "version": _version_body(),
    }
    resp = client.post(f"{PREFIX}/apps", json=body, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_healthz(app_client: TestClient) -> None:
    resp = app_client.get(f"{PREFIX}/healthz")
    assert resp.status_code == 200
    assert resp.json()["service"] == "app-registry"


def test_create_app_with_initial_draft(app_client: TestClient, tenant_headers: dict) -> None:
    app = _create_app(app_client, tenant_headers)
    assert app["id"].startswith("app_")
    assert app["visibility"] == "private"
    assert app["current_version_id"] is None
    assert len(app["versions"]) == 1
    ver = app["versions"][0]
    assert ver["status"] == "draft"
    assert ver["version"] == "1"
    assert ver["id"].startswith("appv_")
    assert ver["tenant_id"]


def test_versions_are_auto_incremented(app_client: TestClient, tenant_headers: dict) -> None:
    app = _create_app(app_client, tenant_headers)
    app_id = app["id"]

    body = {**_version_body(), "version": "999"}
    r = app_client.post(f"{PREFIX}/apps/{app_id}/versions", json=body, headers=tenant_headers)
    assert r.status_code == 201, r.text
    assert r.json()["version"] == "2"

    vid = r.json()["id"]
    r2 = app_client.put(
        f"{PREFIX}/apps/{app_id}/versions/{vid}",
        json={**_version_body(), "version": "1000", "system_prompt": "Updated."},
        headers=tenant_headers,
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["version"] == "2"
    assert r2.json()["system_prompt"] == "Updated."


def test_update_app_fields(app_client: TestClient, tenant_headers: dict) -> None:
    app = _create_app(app_client, tenant_headers)

    resp = app_client.put(
        f"{PREFIX}/apps/{app['id']}",
        json={
            "name": "Contract Risk Reviewer",
            "description": "updated",
            "icon": "R",
            "visibility": "tenant",
        },
        headers=tenant_headers,
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["name"] == "Contract Risk Reviewer"
    assert body["description"] == "updated"
    assert body["icon"] == "R"
    assert body["visibility"] == "tenant"


def test_delete_app_removes_detail(app_client: TestClient, tenant_headers: dict) -> None:
    app = _create_app(app_client, tenant_headers)

    deleted = app_client.delete(f"{PREFIX}/apps/{app['id']}", headers=tenant_headers)
    assert deleted.status_code == 204, deleted.text

    detail = app_client.get(f"{PREFIX}/apps/{app['id']}", headers=tenant_headers)
    assert detail.status_code == 404
    assert detail.json()["code"] == "APP.NOT_FOUND"


async def test_service_non_author_cannot_update_or_delete_app(
    session: AsyncSession,
    tenant_headers: dict,
    other_user_headers: dict,
) -> None:
    author_svc = AppService(
        session,
        tenant_id=tenant_headers["X-Tenant-Id"],
        user_id=tenant_headers["X-User-Id"],
    )
    app = await author_svc.create_app(
        name="Owned App",
        description="d",
        icon="O",
        visibility="private",
        version=AppVersionCreate.model_validate(_version_body()),
    )

    other_svc = AppService(
        session,
        tenant_id=tenant_headers["X-Tenant-Id"],
        user_id=other_user_headers["X-User-Id"],
    )

    with pytest.raises(AppForbiddenError):
        await other_svc.update_app(
            app.id,
            is_reviewer=False,
            name=None,
            description="takeover",
            icon=None,
            visibility=None,
        )
    with pytest.raises(AppForbiddenError):
        await other_svc.delete_app(app.id, is_reviewer=False)


def test_list_mine_and_published_scopes(
    app_client: TestClient,
    tenant_headers: dict,
    other_user_headers: dict,
) -> None:
    mine = _create_app(app_client, tenant_headers)
    other = app_client.post(
        f"{PREFIX}/apps",
        json={"name": "Other App", "version": _version_body()},
        headers=other_user_headers,
    ).json()

    # Publish only the other user's App; it should appear on the tenant shelf.
    other_vid = other["versions"][0]["id"]
    published = app_client.post(
        f"{PREFIX}/apps/{other['id']}/versions/{other_vid}/publish",
        headers=other_user_headers,
    )
    assert published.status_code == 200, published.text

    mine_list = app_client.get(f"{PREFIX}/apps?scope=mine", headers=tenant_headers)
    assert mine_list.status_code == 200
    assert [item["id"] for item in mine_list.json()["items"]] == [mine["id"]]

    shelf = app_client.get(f"{PREFIX}/apps?scope=published", headers=tenant_headers)
    assert shelf.status_code == 200
    assert [item["id"] for item in shelf.json()["items"]] == [other["id"]]


def test_full_review_lifecycle(app_client: TestClient, tenant_headers: dict,
                               internal_headers: dict) -> None:
    app = _create_app(app_client, tenant_headers)
    app_id = app["id"]
    vid = app["versions"][0]["id"]

    # submit draft → pending_review
    r = app_client.post(f"{PREFIX}/apps/{app_id}/versions/{vid}/submit", headers=tenant_headers)
    assert r.status_code == 200
    assert r.json()["status"] == "pending_review"
    assert r.json()["submitted_by"]

    # appears in review queue
    q = app_client.get(f"{PREFIX}/apps/review/pending", headers=tenant_headers)
    assert q.status_code == 200
    assert q.json()["total"] == 1
    assert q.json()["items"][0]["app_name"] == "Contract Reviewer"

    # approve → published + app pointer + visibility
    a = app_client.post(f"{PREFIX}/apps/review/{vid}/approve", json={"note": "lgtm"},
                        headers=tenant_headers)
    assert a.status_code == 200
    assert a.json()["status"] == "published"

    detail = app_client.get(f"{PREFIX}/apps/{app_id}", headers=tenant_headers).json()
    assert detail["current_version_id"] == vid
    assert detail["visibility"] == "tenant"

    # shows up in published list
    pub = app_client.get(f"{PREFIX}/apps?scope=published", headers=tenant_headers).json()
    assert any(a["id"] == app_id for a in pub["items"])

    # internal resolve returns only the published version
    res = app_client.get(
        f"{PREFIX}/internal/apps/{app_id}/resolve?tenant_id={internal_headers['X-Tenant-Id']}",
        headers=internal_headers,
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert len(body["versions"]) == 1
    assert body["versions"][0]["status"] == "published"
    assert body["versions"][0]["system_prompt"]


def test_reject_requires_note(app_client: TestClient, tenant_headers: dict) -> None:
    app = _create_app(app_client, tenant_headers)
    app_id, vid = app["id"], app["versions"][0]["id"]
    app_client.post(f"{PREFIX}/apps/{app_id}/versions/{vid}/submit", headers=tenant_headers)

    r = app_client.post(f"{PREFIX}/apps/review/{vid}/reject", json={}, headers=tenant_headers)
    assert r.status_code == 422
    assert r.json()["code"] == "APP.REVIEW.NOTE_REQUIRED"

    r2 = app_client.post(f"{PREFIX}/apps/review/{vid}/reject", json={"note": "no"},
                         headers=tenant_headers)
    assert r2.status_code == 200
    assert r2.json()["status"] == "rejected"
    assert r2.json()["review_note"] == "no"


def test_edit_non_draft_rejected(app_client: TestClient, tenant_headers: dict) -> None:
    app = _create_app(app_client, tenant_headers)
    app_id, vid = app["id"], app["versions"][0]["id"]
    app_client.post(f"{PREFIX}/apps/{app_id}/versions/{vid}/submit", headers=tenant_headers)

    r = app_client.put(f"{PREFIX}/apps/{app_id}/versions/{vid}", json=_version_body(),
                       headers=tenant_headers)
    assert r.status_code == 409
    assert r.json()["code"] == "APP.VERSION.NOT_DRAFT"


def test_publish_direct(app_client: TestClient, tenant_headers: dict) -> None:
    app = _create_app(app_client, tenant_headers)
    app_id, vid = app["id"], app["versions"][0]["id"]
    r = app_client.post(f"{PREFIX}/apps/{app_id}/versions/{vid}/publish", headers=tenant_headers)
    assert r.status_code == 200
    assert r.json()["status"] == "published"
    detail = app_client.get(f"{PREFIX}/apps/{app_id}", headers=tenant_headers).json()
    assert detail["current_version_id"] == vid


def test_invalid_input_schema_select_without_options(app_client: TestClient,
                                                     tenant_headers: dict) -> None:
    body = {
        "name": "Bad", "version": {
            **_version_body(),
            "input_schema": [{"key": "x", "type": "select", "label": "X"}],
        },
    }
    r = app_client.post(f"{PREFIX}/apps", json=body, headers=tenant_headers)
    assert r.status_code == 422


def test_invalid_input_schema_bad_key(app_client: TestClient, tenant_headers: dict) -> None:
    body = {
        "name": "Bad2", "version": {
            **_version_body(),
            "input_schema": [{"key": "Bad Key", "type": "text", "label": "X"}],
        },
    }
    r = app_client.post(f"{PREFIX}/apps", json=body, headers=tenant_headers)
    assert r.status_code == 422


def test_duplicate_keys_rejected(app_client: TestClient, tenant_headers: dict) -> None:
    body = {
        "name": "Dup", "version": {
            **_version_body(),
            "input_schema": [
                {"key": "a", "type": "text", "label": "A"},
                {"key": "a", "type": "text", "label": "B"},
            ],
        },
    }
    r = app_client.post(f"{PREFIX}/apps", json=body, headers=tenant_headers)
    assert r.status_code == 422


def test_name_conflict(app_client: TestClient, tenant_headers: dict) -> None:
    _create_app(app_client, tenant_headers)
    r = app_client.post(
        f"{PREFIX}/apps",
        json={"name": "Contract Reviewer", "version": _version_body()},
        headers=tenant_headers,
    )
    assert r.status_code == 409
    assert r.json()["code"] == "APP.NAME_CONFLICT"


def test_internal_resolve_requires_scope(app_client: TestClient, tenant_headers: dict) -> None:
    app = _create_app(app_client, tenant_headers)
    app_id = app["id"]
    # missing X-Service-Scopes → 403
    r = app_client.get(
        f"{PREFIX}/internal/apps/{app_id}/resolve?tenant_id={tenant_headers['X-Tenant-Id']}",
        headers=tenant_headers,
    )
    assert r.status_code == 403


def test_internal_resolve_no_published_version(app_client: TestClient, tenant_headers: dict,
                                               internal_headers: dict) -> None:
    app = _create_app(app_client, tenant_headers)  # draft only, never published
    app_id = app["id"]
    r = app_client.get(
        f"{PREFIX}/internal/apps/{app_id}/resolve?tenant_id={internal_headers['X-Tenant-Id']}",
        headers=internal_headers,
    )
    assert r.status_code == 404
    assert r.json()["code"] == "APP.NO_PUBLISHED_VERSION"
