"""Private-App visibility and the privacy surface (GDPR export / erasure).

Runs against in-memory SQLite like ``test_api.py``.  Where it matters who is
a reviewer, authz is replaced by a stand-in that denies ``app.review`` and
allows everything else, so "another member" really is a plain member.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi.testclient import TestClient

from app_registry.services.app_service import ERASED_USER
from tests.conftest import TEST_TENANT, TEST_USER, TEST_USER_OTHER

PREFIX = "/api/apps/v1"
ADMIN = {"X-Actor": "ADMIN"}


def _version_body() -> dict[str, Any]:
    return {
        "version": "1.0.0",
        "system_prompt": "You summarise.",
        "user_prompt_template": "Summarise {{text}}.",
        "model": "claude-opus-4-8",
        "input_schema": [{"key": "text", "type": "textarea", "label": "Text"}],
    }


def _create(client: TestClient, headers: dict[str, str], name: str,
            visibility: str = "private") -> dict[str, Any]:
    resp = client.post(
        f"{PREFIX}/apps",
        json={"name": name, "visibility": visibility, "version": _version_body()},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _publish(client: TestClient, headers: dict[str, str], app: dict[str, Any]) -> None:
    vid = app["versions"][0]["id"]
    resp = client.post(f"{PREFIX}/apps/{app['id']}/versions/{vid}/publish", headers=headers)
    assert resp.status_code == 200, resp.text


@pytest.fixture
def members_are_not_reviewers(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """authz allows every action except ``app.review``."""

    class _Client:
        def __init__(self, *a: Any, **kw: Any) -> None:
            pass

        async def __aenter__(self) -> _Client:
            return self

        async def __aexit__(self, *a: Any) -> None:
            return None

        async def post(self, *a: Any, json: dict[str, Any], **kw: Any) -> MagicMock:
            resp = MagicMock(spec=httpx.Response)
            resp.status_code = 200
            resp.json = MagicMock(return_value={"allowed": json["action"] != "app.review"})
            return resp

    monkeypatch.setattr("app_registry.api.authz_guard.httpx.AsyncClient", _Client)
    yield


# ── visibility ───────────────────────────────────────────────────────────────


def test_publish_keeps_a_private_app_private(
    app_client: TestClient, tenant_headers: dict[str, str],
) -> None:
    app = _create(app_client, tenant_headers, "Mine")
    _publish(app_client, tenant_headers, app)

    detail = app_client.get(f"{PREFIX}/apps/{app['id']}", headers=tenant_headers).json()
    assert detail["visibility"] == "private"
    shelf = app_client.get(f"{PREFIX}/apps?scope=published", headers=tenant_headers).json()
    assert shelf["items"] == []


def test_publish_puts_a_tenant_app_on_the_shelf(
    app_client: TestClient, tenant_headers: dict[str, str],
) -> None:
    app = _create(app_client, tenant_headers, "Shared", visibility="tenant")
    _publish(app_client, tenant_headers, app)

    shelf = app_client.get(f"{PREFIX}/apps?scope=published", headers=tenant_headers).json()
    assert [item["id"] for item in shelf["items"]] == [app["id"]]


def test_a_private_app_does_not_exist_for_another_member(
    app_client: TestClient,
    tenant_headers: dict[str, str],
    other_user_headers: dict[str, str],
    members_are_not_reviewers: None,
) -> None:
    app = _create(app_client, tenant_headers, "Mine")
    _publish(app_client, {**tenant_headers, **ADMIN}, app)

    other = app_client.get(f"{PREFIX}/apps/{app['id']}", headers=other_user_headers)
    assert other.status_code == 404
    assert other.json()["code"] == "APP.NOT_FOUND"
    # The owner and a reviewer still see it.
    assert app_client.get(f"{PREFIX}/apps/{app['id']}", headers=tenant_headers).status_code == 200
    assert app_client.get(
        f"{PREFIX}/apps/{app['id']}", headers={**other_user_headers, **ADMIN},
    ).status_code == 200


def test_a_shared_app_is_readable_by_another_member(
    app_client: TestClient,
    tenant_headers: dict[str, str],
    other_user_headers: dict[str, str],
    members_are_not_reviewers: None,
) -> None:
    app = _create(app_client, tenant_headers, "Shared", visibility="tenant")
    _publish(app_client, {**tenant_headers, **ADMIN}, app)

    other = app_client.get(f"{PREFIX}/apps/{app['id']}", headers=other_user_headers)
    assert other.status_code == 200
    assert [v["id"] for v in other.json()["versions"]] == [app["versions"][0]["id"]]


# ── export ───────────────────────────────────────────────────────────────────


def test_export_lists_authored_apps_and_review_trail(
    app_client: TestClient,
    tenant_headers: dict[str, str],
    other_user_headers: dict[str, str],
) -> None:
    mine = _create(app_client, tenant_headers, "Mine")
    theirs = _create(app_client, other_user_headers, "Theirs", visibility="tenant")
    theirs_vid = theirs["versions"][0]["id"]
    # The other member submits; the subject approves (review trail names them).
    app_client.post(
        f"{PREFIX}/apps/{theirs['id']}/versions/{theirs_vid}/submit", headers=other_user_headers,
    )
    approved = app_client.post(
        f"{PREFIX}/apps/review/{theirs_vid}/approve", json={"note": "fine"},
        headers=tenant_headers,
    )
    assert approved.status_code == 200, approved.text

    resp = app_client.get(f"{PREFIX}/privacy/apps", headers=tenant_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["subject_user_id"] == TEST_USER
    assert [a["id"] for a in body["apps"]] == [mine["id"]]
    assert body["apps"][0]["versions"][0]["system_prompt"] == "You summarise."
    assert len(body["review_trail"]) == 1
    entry = body["review_trail"][0]
    assert entry["version_id"] == theirs_vid
    assert entry["review_note"] == "fine"
    assert entry["reviewed_at"] is not None
    # Fields about the other member are not part of the subject's export.
    assert entry["submitted_at"] is None and entry["created_at"] is None


def test_non_admin_cannot_name_another_subject(
    app_client: TestClient, tenant_headers: dict[str, str],
) -> None:
    for resp in (
        app_client.get(f"{PREFIX}/privacy/apps?user_id={TEST_USER_OTHER}", headers=tenant_headers),
        app_client.post(
            f"{PREFIX}/privacy/apps/erase", json={"user_id": TEST_USER_OTHER},
            headers=tenant_headers,
        ),
    ):
        assert resp.status_code == 403
        assert resp.json()["code"] == "APP.PRIVACY.FORBIDDEN"


def test_admin_exports_a_named_subject(
    app_client: TestClient,
    tenant_headers: dict[str, str],
    other_user_headers: dict[str, str],
) -> None:
    theirs = _create(app_client, other_user_headers, "Theirs")
    resp = app_client.get(
        f"{PREFIX}/privacy/apps?user_id={TEST_USER_OTHER}", headers={**tenant_headers, **ADMIN},
    )
    assert resp.status_code == 200, resp.text
    assert [a["id"] for a in resp.json()["apps"]] == [theirs["id"]]


def test_privacy_routes_need_no_role_permission(
    app_client: TestClient, tenant_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Access and erasure are the subject's rights: authz is never consulted."""

    class _MustNotCall:
        def __init__(self, *a: Any, **kw: Any) -> None:
            raise AssertionError("privacy routes must not call authz /check")

    monkeypatch.setattr("app_registry.api.authz_guard.httpx.AsyncClient", _MustNotCall)
    assert app_client.get(f"{PREFIX}/privacy/apps", headers=tenant_headers).status_code == 200
    assert app_client.post(
        f"{PREFIX}/privacy/apps/erase", json={"dry_run": True}, headers=tenant_headers,
    ).status_code == 200


@pytest.mark.parametrize("user_id", ["SYSTEM", "not-a-user"])
def test_privacy_routes_require_an_identified_caller(
    app_client: TestClient, user_id: str,
) -> None:
    headers = {"X-Tenant-Id": TEST_TENANT, "X-User-Id": user_id}
    assert app_client.get(f"{PREFIX}/privacy/apps", headers=headers).status_code == 403
    assert app_client.post(f"{PREFIX}/privacy/apps/erase", headers=headers).status_code == 403


# ── erasure ──────────────────────────────────────────────────────────────────


def _erase(client: TestClient, headers: dict[str, str], **body: Any) -> dict[str, Any]:
    resp = client.post(f"{PREFIX}/privacy/apps/erase", json=body, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_erasure_deletes_unshared_apps_and_deattributes_shared_ones(
    app_client: TestClient,
    tenant_headers: dict[str, str],
    other_user_headers: dict[str, str],
) -> None:
    private = _create(app_client, tenant_headers, "Private")
    unpublished = _create(app_client, tenant_headers, "Tenant draft", visibility="tenant")
    shared = _create(app_client, tenant_headers, "Shared", visibility="tenant")
    _publish(app_client, tenant_headers, shared)
    theirs = _create(app_client, other_user_headers, "Theirs", visibility="tenant")
    theirs_vid = theirs["versions"][0]["id"]
    app_client.post(
        f"{PREFIX}/apps/{theirs['id']}/versions/{theirs_vid}/submit", headers=other_user_headers,
    )
    app_client.post(
        f"{PREFIX}/apps/review/{theirs_vid}/approve", json={"note": "ok"}, headers=tenant_headers,
    )

    expected = {"apps_deleted": 2, "apps_deattributed": 1, "versions_deattributed": 2}
    preview = _erase(app_client, tenant_headers, dry_run=True)
    assert preview["dry_run"] is True and preview["deleted"] == expected
    # A dry run changes nothing.
    assert app_client.get(
        f"{PREFIX}/apps/{private['id']}", headers=tenant_headers,
    ).status_code == 200

    done = _erase(app_client, tenant_headers)
    assert done["dry_run"] is False and done["deleted"] == expected

    for gone in (private, unpublished):
        resp = app_client.get(f"{PREFIX}/apps/{gone['id']}", headers=tenant_headers)
        assert resp.status_code == 404
    kept = app_client.get(f"{PREFIX}/apps/{shared['id']}", headers=tenant_headers).json()
    assert kept["author_id"] == ERASED_USER
    assert kept["versions"][0]["created_by"] == ERASED_USER
    assert kept["versions"][0]["reviewed_by"] == ERASED_USER
    reviewed = app_client.get(f"{PREFIX}/apps/{theirs['id']}", headers=other_user_headers).json()
    assert reviewed["author_id"] == TEST_USER_OTHER
    assert reviewed["versions"][0]["reviewed_by"] == ERASED_USER
    # The subject's own words go too; the version and its status stay.
    assert reviewed["versions"][0]["review_note"] is None
    assert reviewed["versions"][0]["status"] == "published"
    assert reviewed["versions"][0]["submitted_by"] == TEST_USER_OTHER

    # Nothing names the subject any more, so a repeat finds nothing to do.
    again = _erase(app_client, tenant_headers)
    assert again["deleted"] == {
        "apps_deleted": 0, "apps_deattributed": 0, "versions_deattributed": 0,
    }
    export = app_client.get(f"{PREFIX}/privacy/apps", headers=tenant_headers).json()
    assert export["apps"] == [] and export["review_trail"] == []


def test_admin_erases_a_named_subject(
    app_client: TestClient,
    tenant_headers: dict[str, str],
    other_user_headers: dict[str, str],
) -> None:
    theirs = _create(app_client, other_user_headers, "Theirs")
    body = _erase(app_client, {**tenant_headers, **ADMIN}, user_id=TEST_USER_OTHER)
    assert body["subject_user_id"] == TEST_USER_OTHER
    assert body["deleted"]["apps_deleted"] == 1
    assert app_client.get(
        f"{PREFIX}/apps/{theirs['id']}", headers=other_user_headers,
    ).status_code == 404
