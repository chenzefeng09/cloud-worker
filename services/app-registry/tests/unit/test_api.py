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


def _create_named_app(
    client: TestClient, headers: dict, name: str, tags: list[str] | None = None,
) -> dict:
    body: dict = {"name": name, "version": _version_body()}
    if tags is not None:
        body["tags"] = tags
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


def test_create_app_accepts_empty_prompts(app_client: TestClient, tenant_headers: dict) -> None:
    """The "new App" modal creates an initial draft with both prompts empty.

    ``system_prompt`` stays optional forever (a published App may rely on the
    worker's default persona); ``user_prompt_template`` is only enforced by the
    editor before publish, so the storage layer must accept "" as well.
    """
    body = {
        "name": "Empty Prompts",
        "version": {**_version_body(), "system_prompt": "", "user_prompt_template": ""},
    }
    r = app_client.post(f"{PREFIX}/apps", json=body, headers=tenant_headers)
    assert r.status_code == 201, r.text
    ver = r.json()["versions"][0]
    assert ver["system_prompt"] == ""
    assert ver["user_prompt_template"] == ""

    # …and the same holds when editing the draft in place.
    app_id, vid = r.json()["id"], ver["id"]
    r2 = app_client.put(
        f"{PREFIX}/apps/{app_id}/versions/{vid}",
        json={**_version_body(), "system_prompt": "", "user_prompt_template": "Do it."},
        headers=tenant_headers,
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["system_prompt"] == ""


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


# ---------------------------------------------------------------------------
# tags
# ---------------------------------------------------------------------------
def test_create_app_with_tags(app_client: TestClient, tenant_headers: dict) -> None:
    app = _create_named_app(
        app_client, tenant_headers, "Tagged App", tags=["legal", "contract"]
    )
    assert app["tags"] == ["legal", "contract"]


def test_create_app_defaults_to_empty_tags(
    app_client: TestClient, tenant_headers: dict,
) -> None:
    app = _create_named_app(app_client, tenant_headers, "Untagged App")
    assert app["tags"] == []


def test_create_app_tags_dedup_and_strip(
    app_client: TestClient, tenant_headers: dict,
) -> None:
    app = _create_named_app(
        app_client, tenant_headers, "Dedup App",
        tags=["  legal  ", "legal", "contract"],
    )
    # Duplicates removed (case-sensitive), surrounding whitespace stripped.
    assert app["tags"] == ["legal", "contract"]


def test_update_app_tags(app_client: TestClient, tenant_headers: dict) -> None:
    app = _create_named_app(app_client, tenant_headers, "Update Tags", tags=["a"])
    resp = app_client.put(
        f"{PREFIX}/apps/{app['id']}",
        json={"tags": ["a", "b"]},
        headers=tenant_headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["tags"] == ["a", "b"]


def test_update_app_tags_clear_with_empty_list(
    app_client: TestClient, tenant_headers: dict,
) -> None:
    app = _create_named_app(app_client, tenant_headers, "Clear Tags", tags=["a"])
    resp = app_client.put(
        f"{PREFIX}/apps/{app['id']}",
        json={"tags": []},
        headers=tenant_headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["tags"] == []


def test_update_app_omitting_tags_leaves_unchanged(
    app_client: TestClient, tenant_headers: dict,
) -> None:
    app = _create_named_app(app_client, tenant_headers, "Keep Tags", tags=["a", "b"])
    # PATCH-style update touching only name; tags must be preserved.
    resp = app_client.put(
        f"{PREFIX}/apps/{app['id']}",
        json={"name": "Keep Tags Renamed"},
        headers=tenant_headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["name"] == "Keep Tags Renamed"
    assert body["tags"] == ["a", "b"]


def test_list_apps_filter_by_single_tag(
    app_client: TestClient, tenant_headers: dict,
) -> None:
    a = _create_named_app(app_client, tenant_headers, "App A", tags=["foo", "bar"])
    _create_named_app(app_client, tenant_headers, "App B", tags=["baz"])
    _create_named_app(app_client, tenant_headers, "App C")  # no tags

    resp = app_client.get(
        f"{PREFIX}/apps?scope=mine&tag=foo", headers=tenant_headers,
    )
    assert resp.status_code == 200, resp.text
    ids = [item["id"] for item in resp.json()["items"]]
    assert ids == [a["id"]]


def test_list_apps_filter_by_multiple_tags_or(
    app_client: TestClient, tenant_headers: dict,
) -> None:
    a = _create_named_app(app_client, tenant_headers, "App A", tags=["foo"])
    b = _create_named_app(app_client, tenant_headers, "App B", tags=["bar", "baz"])
    _create_named_app(app_client, tenant_headers, "App C")  # no tags

    # ?tag=foo&tag=baz -> OR: App A (foo) and App B (baz), ordered by name asc.
    resp = app_client.get(
        f"{PREFIX}/apps?scope=mine&tag=foo&tag=baz", headers=tenant_headers,
    )
    assert resp.status_code == 200, resp.text
    ids = [item["id"] for item in resp.json()["items"]]
    assert ids == [a["id"], b["id"]]


def test_list_apps_tag_filter_no_match(
    app_client: TestClient, tenant_headers: dict,
) -> None:
    _create_named_app(app_client, tenant_headers, "App A", tags=["foo"])
    resp = app_client.get(
        f"{PREFIX}/apps?scope=mine&tag=missing", headers=tenant_headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["items"] == []


def test_list_apps_no_tag_filter_returns_all(
    app_client: TestClient, tenant_headers: dict,
) -> None:
    _create_named_app(app_client, tenant_headers, "App A", tags=["foo"])
    _create_named_app(app_client, tenant_headers, "App B")
    resp = app_client.get(f"{PREFIX}/apps?scope=mine", headers=tenant_headers)
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["items"]) == 2


def test_create_app_empty_tag_rejected(app_client: TestClient, tenant_headers: dict) -> None:
    r = _create_named_app_raw(
        app_client, tenant_headers, "Bad Empty", tags=["ok", "   "],
    )
    assert r.status_code == 422


def test_create_app_tag_too_long_rejected(
    app_client: TestClient, tenant_headers: dict,
) -> None:
    r = _create_named_app_raw(
        app_client, tenant_headers, "Bad Long", tags=["x" * 33],
    )
    assert r.status_code == 422


def test_create_app_too_many_tags_rejected(
    app_client: TestClient, tenant_headers: dict,
) -> None:
    # 21 distinct tags exceeds the max of 20.
    r = _create_named_app_raw(
        app_client, tenant_headers, "Bad Many",
        tags=[f"t{i}" for i in range(21)],
    )
    assert r.status_code == 422


def _create_named_app_raw(
    client: TestClient, headers: dict, name: str, tags: list[str],
):
    """Like _create_named_app but returns the raw response (for 422 assertions)."""
    body = {"name": name, "version": _version_body(), "tags": tags}
    return client.post(f"{PREFIX}/apps", json=body, headers=headers)
