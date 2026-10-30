"""Tests: BFF lifecycle admin proxy.

Covers for every endpoint: unauthenticated 401/403, authz-denied 403,
super_admin passthrough 2xx, downstream 4xx 透传, downstream unreachable 502,
cross-tenant ?tenant_id= header override.

Plus lifecycle specifics: the ``resource:action`` colon routes (notably
``/recycle-bin/{bin_id}:restore``) match correctly and forward the verbatim
colon path upstream; hard-delete and release relay the upstream 202 (approval
request opened) and 200 (already done) unchanged; the four-eyes approval routes
forward their filters and the caller, and the upstream's 403/409 refusals reach
the client with their messages.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from agent_bff.config import Settings
from agent_bff.main import create_app

PREFIX = "/api/bff/v1"
TEST_COOKIE_SECRET = "abcdefghijklmnopqrstuvwxyz0123456789ABCDEFGHIJKL"

LIFECYCLE_URL = "http://lifecycle-test/api/lifecycle/v1"
CALLER_TENANT = "tnt_01J5TEST00000000000000TNT1"
CALLER_USER = "usr_01J5TEST0000000000000USER1"
APPROVAL_ID = "apr_01J9ZZZZZZZZZZZZZZZZZZZZZZ"


def _approval(operation: str, target_id: str, status: str = "PENDING") -> dict:
    """An ``ApprovalRequest`` as foundation lifecycle serialises it."""
    decided = status in ("APPROVED", "REJECTED")
    return {
        "id": APPROVAL_ID,
        "operation": operation,
        "target_id": target_id,
        "status": status,
        "requested_by": "usr_01J5TEST0000000000000OTHER",
        "requested_at": "2026-10-30T10:00:00+00:00",
        "expires_at": "2026-10-30T10:30:00+00:00",
        "decided_by": CALLER_USER if decided else None,
        "decided_at": "2026-10-30T10:05:00+00:00" if decided else None,
    }


def _problem(status: int, code: str, detail: str, user_message: str) -> httpx.Response:
    """A foundation lifecycle RFC 9457 refusal as the upstream sends it."""
    return httpx.Response(
        status,
        json={
            "type": f"https://errors.foundation.example.com/{code.lower().replace('.', '/')}",
            "title": detail,
            "status": status,
            "code": code,
            "detail": detail,
            "user_message": user_message,
            "approval_request_id": APPROVAL_ID,
        },
        headers={"content-type": "application/problem+json"},
    )


@pytest.fixture
def lc_settings() -> Settings:
    return Settings(
        env="test",
        log_level="WARNING",
        cookie_secret=TEST_COOKIE_SECRET,
        cookie_secure=False,
        foundation_identity_url="http://identity-test/api/identity/v1",
        session_orchestrator_url="http://orch-test:8006/api/session/v1",
        foundation_lifecycle_url=LIFECYCLE_URL,
        cors_origins="http://localhost",
        otel_enabled=False,
    )


@pytest.fixture
def lc_client(lc_settings: Settings) -> Iterator[TestClient]:
    app = create_app(lc_settings)
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture
def lc_cookie(lc_settings: Settings) -> dict[str, str]:
    from foundation_sdk import CookieSigner, CookieSignerSettings

    signer = CookieSigner(
        CookieSignerSettings(
            secret_key=lc_settings.cookie_secret,
            salt="agent-bff-session",
            max_age_seconds=lc_settings.cookie_max_age,
        ),
    )
    token = signer.sign(
        {
            "user_id": CALLER_USER,
            "tenant_id": CALLER_TENANT,
            "access_token": "fake-jwt-for-test",
        },
    )
    return {lc_settings.cookie_name: token}


class FakeLifecycle:
    def __init__(
        self,
        *,
        authz_allowed: bool = True,
        error_status: int | None = None,
        responses: dict[str, httpx.Response] | None = None,
    ) -> None:
        self.authz_allowed = authz_allowed
        self.error_status = error_status
        # Upstream URL suffix → the answer for it, ahead of the defaults below.
        self.responses = responses or {}
        self.requests: list[tuple[str, str, dict, dict]] = []
        # (url, kwargs) per upstream call: whether a body was sent, and which.
        self.sent: list[tuple[str, dict[str, Any]]] = []

    def route(self, method: str, url: str, **kw: Any) -> httpx.Response:
        self.requests.append(
            (method, url, dict(kw.get("headers") or {}), dict(kw.get("params") or {})),
        )
        self.sent.append((url, kw))

        if url.endswith("/check"):
            return httpx.Response(200, json={"allowed": self.authz_allowed})

        for suffix, answer in self.responses.items():
            if url.endswith(suffix):
                return answer

        if self.error_status is not None:
            return httpx.Response(
                self.error_status,
                json={"title": "upstream error", "status": self.error_status},
            )

        if ":soft-delete" in url:
            return httpx.Response(200, json={"bin_id": "bin_new", "status": "SOFT_DELETED"})
        if ":restore" in url:
            return httpx.Response(200, json={"bin_id": "bin_1", "status": "RESTORED"})
        if ":hard-delete" in url:
            return httpx.Response(
                202,
                json={"destroyed": False, "approval_request": _approval("HARD_DELETE", "bin_1")},
            )
        if "/recycle-bin" in url:
            return httpx.Response(200, json={"items": [], "next_cursor": None})
        if "/sweep" in url:
            return httpx.Response(200, json={"swept": 0})
        if "legal-holds:check" in url:
            return httpx.Response(200, json={"on_hold": False})
        if ":release" in url:
            return httpx.Response(
                202,
                json={
                    "released": False,
                    "approval_request": _approval("RELEASE_LEGAL_HOLD", "hold_1"),
                },
            )
        if url.endswith("/approve"):
            return httpx.Response(200, json=_approval("HARD_DELETE", "bin_1", "APPROVED"))
        if url.endswith("/reject"):
            return httpx.Response(200, json=_approval("HARD_DELETE", "bin_1", "REJECTED"))
        if "/approval-requests/" in url:
            return httpx.Response(200, json=_approval("HARD_DELETE", "bin_1"))
        if "/approval-requests" in url:
            return httpx.Response(
                200, json={"items": [_approval("HARD_DELETE", "bin_1")], "next_cursor": None},
            )
        if "/legal-holds" in url:
            if method == "POST":
                return httpx.Response(201, json={"hold_id": "hold_new"})
            return httpx.Response(200, json={"items": [], "next_cursor": None})

        if "/storage-quotas/admin/summary" in url:
            return httpx.Response(200, json={"items": [], "total": 0})
        if "/storage-quotas/admin/" in url and method == "PATCH":
            return httpx.Response(
                200,
                json={
                    "tenant_id": "tnt_X",
                    "bucket_purpose": "workspace",
                    "limit_bytes": 1024,
                    "soft_warn_pct": 80,
                },
            )
        if "/orphan-reports/" in url:
            return httpx.Response(200, json={"id": "orr_1", "dry_run": True})
        if "/orphan-reports" in url:
            return httpx.Response(200, json={"items": [], "next_cursor": None})

        return httpx.Response(200, json={"ok": True})


def _install(fake: FakeLifecycle, monkeypatch: pytest.MonkeyPatch) -> FakeLifecycle:
    class _AC:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        async def get(self, url, **kw): return fake.route("GET", url, **kw)
        async def post(self, url, **kw): return fake.route("POST", url, **kw)
        async def put(self, url, **kw): return fake.route("PUT", url, **kw)
        async def patch(self, url, **kw): return fake.route("PATCH", url, **kw)
        async def delete(self, url, **kw): return fake.route("DELETE", url, **kw)

    monkeypatch.setattr(httpx, "AsyncClient", _AC)
    return fake


@pytest.fixture
def lc_allowed(monkeypatch: pytest.MonkeyPatch) -> FakeLifecycle:
    return _install(FakeLifecycle(authz_allowed=True), monkeypatch)


@pytest.fixture
def lc_denied(monkeypatch: pytest.MonkeyPatch) -> FakeLifecycle:
    return _install(FakeLifecycle(authz_allowed=False), monkeypatch)


@pytest.fixture
def lc_404(monkeypatch: pytest.MonkeyPatch) -> FakeLifecycle:
    return _install(FakeLifecycle(authz_allowed=True, error_status=404), monkeypatch)


@pytest.fixture
def lc_unreachable(monkeypatch: pytest.MonkeyPatch) -> FakeLifecycle:
    fake = FakeLifecycle(authz_allowed=True)

    class _AC:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None

        async def _maybe(self, method, url, **kw):
            if url.endswith("/check"):
                return fake.route(method, url, **kw)
            raise httpx.ConnectError("downstream down")

        async def get(self, url, **kw): return await self._maybe("GET", url, **kw)
        async def post(self, url, **kw): return await self._maybe("POST", url, **kw)
        async def put(self, url, **kw): return await self._maybe("PUT", url, **kw)
        async def patch(self, url, **kw): return await self._maybe("PATCH", url, **kw)
        async def delete(self, url, **kw): return await self._maybe("DELETE", url, **kw)

    monkeypatch.setattr(httpx, "AsyncClient", _AC)
    return fake


# (method, path, json-body, happy-status)
ENDPOINTS = [
    ("POST",   "/admin/lifecycle/recycle-bin:soft-delete", {"resource_type_id": "t", "resource_id": "r", "resource_name": "n"}, 200),
    ("POST",   "/admin/lifecycle/recycle-bin/bin_1:restore", {}, 200),
    ("POST",   "/admin/lifecycle/recycle-bin/bin_1:hard-delete", None, 202),
    ("GET",    "/admin/lifecycle/recycle-bin", None, 200),
    ("POST",   "/admin/lifecycle/sweep", {}, 200),
    ("POST",   "/admin/lifecycle/legal-holds", {"resource_type_id": "t", "resource_id": "r", "reason": "x", "case_reference": "c"}, 201),
    ("POST",   "/admin/lifecycle/legal-holds/hold_1:release", None, 202),
    ("GET",    "/admin/lifecycle/legal-holds:check?resource_type_id=t&resource_id=r", None, 200),
    ("GET",    "/admin/lifecycle/legal-holds", None, 200),
    ("GET",    "/admin/lifecycle/approval-requests?filter.status=PENDING", None, 200),
    ("GET",    f"/admin/lifecycle/approval-requests/{APPROVAL_ID}", None, 200),
    ("POST",   f"/admin/lifecycle/approval-requests/{APPROVAL_ID}/approve", None, 200),
    ("POST",   f"/admin/lifecycle/approval-requests/{APPROVAL_ID}/reject", None, 200),
    ("GET",    "/admin/lifecycle/storage-quotas/summary", None, 200),
    ("PATCH",  "/admin/lifecycle/storage-quotas/tnt_X/workspace", {"limit_bytes": 1024}, 200),
    ("GET",    "/admin/lifecycle/orphan-reports?limit=20", None, 200),
    ("GET",    "/admin/lifecycle/orphan-reports/orr_1", None, 200),
]


def _call(client: TestClient, method: str, path: str, body, **kw):
    fn = getattr(client, method.lower())
    if body is not None and method in ("POST", "PUT", "PATCH"):
        return fn(PREFIX + path, json=body, **kw)
    return fn(PREFIX + path, **kw)


@pytest.mark.parametrize("method,path,body,_ok", ENDPOINTS)
def test_unauthenticated_rejected(lc_client, method, path, body, _ok) -> None:
    resp = _call(lc_client, method, path, body)
    assert resp.status_code in (401, 403), f"{method} {path} → {resp.status_code}"


@pytest.mark.parametrize("method,path,body,_ok", ENDPOINTS)
def test_authz_denied_403(lc_client, lc_cookie, lc_denied, method, path, body, _ok) -> None:
    resp = _call(lc_client, method, path, body, cookies=lc_cookie)
    assert resp.status_code == 403, f"{method} {path} → {resp.status_code}"


@pytest.mark.parametrize("method,path,body,ok", ENDPOINTS)
def test_happy_path_passthrough(lc_client, lc_cookie, lc_allowed, method, path, body, ok) -> None:
    resp = _call(lc_client, method, path, body, cookies=lc_cookie)
    assert resp.status_code == ok, f"{method} {path} → {resp.status_code} ({resp.text})"


@pytest.mark.parametrize("method,path,body,_ok", ENDPOINTS)
def test_downstream_4xx_passthrough(lc_client, lc_cookie, lc_404, method, path, body, _ok) -> None:
    resp = _call(lc_client, method, path, body, cookies=lc_cookie)
    assert resp.status_code == 404, f"{method} {path} → {resp.status_code}"


@pytest.mark.parametrize("method,path,body,_ok", ENDPOINTS)
def test_downstream_unreachable_502(lc_client, lc_cookie, lc_unreachable, method, path, body, _ok) -> None:
    resp = _call(lc_client, method, path, body, cookies=lc_cookie)
    assert resp.status_code == 502, f"{method} {path} → {resp.status_code}"


def test_hits_lifecycle_url(lc_client, lc_cookie, lc_allowed) -> None:
    lc_client.get(PREFIX + "/admin/lifecycle/recycle-bin", cookies=lc_cookie)
    assert any(
        u == f"{LIFECYCLE_URL}/recycle-bin" for (_, u, _, _) in lc_allowed.requests
    )


def test_colon_restore_route_matches_and_extracts_bin_id(lc_client, lc_cookie, lc_allowed) -> None:
    """The ``{bin_id}:restore`` colon route matches and the path param lands in
    the verbatim upstream URL (httpx does not encode the colon)."""
    resp = lc_client.post(
        PREFIX + "/admin/lifecycle/recycle-bin/bin_ABC:restore", json={}, cookies=lc_cookie,
    )
    assert resp.status_code == 200, resp.text
    urls = [u for (_, u, _, _) in lc_allowed.requests if ":restore" in u]
    assert urls == [f"{LIFECYCLE_URL}/recycle-bin/bin_ABC:restore"]


def test_colon_check_route_does_not_shadow_list(lc_client, lc_cookie, lc_allowed) -> None:
    """``/legal-holds:check`` and ``/legal-holds`` are distinct routes."""
    lc_client.get(
        PREFIX + "/admin/lifecycle/legal-holds:check?resource_type_id=t&resource_id=r",
        cookies=lc_cookie,
    )
    check_calls = [
        (u, p) for (_, u, _, p) in lc_allowed.requests if "legal-holds:check" in u
    ]
    assert check_calls
    url, params = check_calls[-1]
    assert url == f"{LIFECYCLE_URL}/legal-holds:check"
    assert params.get("resource_type_id") == "t"
    assert params.get("resource_id") == "r"


def test_tenant_id_override_changes_header(lc_client, lc_cookie, lc_allowed) -> None:
    target = "tnt_OTHER0000000000000000000001"
    lc_client.get(
        PREFIX + f"/admin/lifecycle/recycle-bin?tenant_id={target}", cookies=lc_cookie,
    )
    calls = [
        (h, p) for (_, u, h, p) in lc_allowed.requests
        if u == f"{LIFECYCLE_URL}/recycle-bin"
    ]
    assert calls
    headers, params = calls[-1]
    assert headers.get("X-Tenant-Id") == target
    assert "tenant_id" not in params


def test_tenant_id_absent_uses_caller_tenant(lc_client, lc_cookie, lc_allowed) -> None:
    lc_client.get(PREFIX + "/admin/lifecycle/recycle-bin", cookies=lc_cookie)
    calls = [
        h for (_, u, h, _) in lc_allowed.requests if u == f"{LIFECYCLE_URL}/recycle-bin"
    ]
    assert calls and calls[-1].get("X-Tenant-Id") == CALLER_TENANT


def test_user_id_forwarded(lc_client, lc_cookie, lc_allowed) -> None:
    lc_client.get(PREFIX + "/admin/lifecycle/recycle-bin", cookies=lc_cookie)
    calls = [
        h for (_, u, h, _) in lc_allowed.requests if u == f"{LIFECYCLE_URL}/recycle-bin"
    ]
    assert calls and calls[-1].get("X-User-Id") == CALLER_USER


def test_storage_admin_routes_send_scopes_header(
    lc_client, lc_cookie, lc_allowed,
) -> None:
    """Storage admin (summary / patch / orphan-reports) forwards
    X-Foundation-Scopes; the unrelated recycle-bin route does not."""
    lc_client.get(
        PREFIX + "/admin/lifecycle/storage-quotas/summary", cookies=lc_cookie,
    )
    lc_client.patch(
        PREFIX + "/admin/lifecycle/storage-quotas/tnt_X/workspace",
        json={"limit_bytes": 1024},
        cookies=lc_cookie,
    )
    lc_client.get(PREFIX + "/admin/lifecycle/orphan-reports", cookies=lc_cookie)
    lc_client.get(PREFIX + "/admin/lifecycle/recycle-bin", cookies=lc_cookie)

    scoped_urls = {
        f"{LIFECYCLE_URL}/storage-quotas/admin/summary",
        f"{LIFECYCLE_URL}/storage-quotas/admin/tnt_X/workspace",
        f"{LIFECYCLE_URL}/orphan-reports",
    }
    for _, url, headers, _ in lc_allowed.requests:
        if url in scoped_urls:
            scopes = headers.get("X-Foundation-Scopes", "")
            assert "storage.admin" in scopes, (url, scopes)
            assert "storage.quota.write" in scopes, (url, scopes)
        elif url == f"{LIFECYCLE_URL}/recycle-bin":
            # Non-scoped surface — header must NOT leak there.
            assert "X-Foundation-Scopes" not in headers, headers


# ── Four-eyes: hard-delete / release relay the upstream status ──────────────────

def test_hard_delete_relays_202_with_the_approval_request(lc_client, lc_cookie, lc_allowed) -> None:
    resp = lc_client.post(
        PREFIX + "/admin/lifecycle/recycle-bin/bin_1:hard-delete", cookies=lc_cookie,
    )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["destroyed"] is False
    assert body["approval_request"]["id"] == APPROVAL_ID
    assert body["approval_request"]["operation"] == "HARD_DELETE"
    assert body["approval_request"]["expires_at"] == "2026-10-30T10:30:00+00:00"


def test_hard_delete_relays_200_when_already_destroyed(lc_client, lc_cookie, monkeypatch) -> None:
    _install(
        FakeLifecycle(responses={":hard-delete": httpx.Response(200, json={"destroyed": True})}),
        monkeypatch,
    )
    resp = lc_client.post(
        PREFIX + "/admin/lifecycle/recycle-bin/bin_1:hard-delete", cookies=lc_cookie,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"destroyed": True}


def test_release_relays_202_with_the_approval_request(lc_client, lc_cookie, lc_allowed) -> None:
    resp = lc_client.post(
        PREFIX + "/admin/lifecycle/legal-holds/hold_1:release", cookies=lc_cookie,
    )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["released"] is False
    assert body["approval_request"]["operation"] == "RELEASE_LEGAL_HOLD"
    assert body["approval_request"]["target_id"] == "hold_1"
    assert body["approval_request"]["expires_at"] == "2026-10-30T10:30:00+00:00"


def test_release_relays_200_when_already_released(lc_client, lc_cookie, monkeypatch) -> None:
    _install(
        FakeLifecycle(responses={":release": httpx.Response(200, json={"released": True})}),
        monkeypatch,
    )
    resp = lc_client.post(
        PREFIX + "/admin/lifecycle/legal-holds/hold_1:release", cookies=lc_cookie,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"released": True}


def test_hard_delete_under_legal_hold_409_reaches_client(lc_client, lc_cookie, monkeypatch) -> None:
    code = "FOUNDATION.LIFECYCLE.STATE.LEGAL_HOLD_ACTIVE"
    _install(
        FakeLifecycle(responses={":hard-delete": _problem(
            409, code, "Cannot delete resource with active legal hold.",
            "This resource is under a legal hold.",
        )}),
        monkeypatch,
    )
    resp = lc_client.post(
        PREFIX + "/admin/lifecycle/recycle-bin/bin_1:hard-delete", cookies=lc_cookie,
    )
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["user_message"] == "This resource is under a legal hold."
    assert detail["detail"]["code"] == code


# ── Four-eyes: approval-request routes ──────────────────────────────────────────

def test_approval_list_forwards_filters_and_cursor(lc_client, lc_cookie, lc_allowed) -> None:
    target = "tnt_OTHER0000000000000000000001"
    resp = lc_client.get(
        PREFIX + "/admin/lifecycle/approval-requests",
        params={
            "filter.status": "PENDING",
            "filter.operation": "RELEASE_LEGAL_HOLD",
            "cursor": "c1",
            "limit": "20",
            "tenant_id": target,
        },
        cookies=lc_cookie,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["items"][0]["id"] == APPROVAL_ID
    calls = [
        (h, p) for (_, u, h, p) in lc_allowed.requests
        if u == f"{LIFECYCLE_URL}/approval-requests"
    ]
    assert calls
    headers, params = calls[-1]
    assert params == {
        "filter.status": "PENDING",
        "filter.operation": "RELEASE_LEGAL_HOLD",
        "cursor": "c1",
        "limit": "20",
    }
    assert headers.get("X-Tenant-Id") == target
    assert headers.get("X-User-Id") == CALLER_USER


def test_get_approval_request_hits_upstream(lc_client, lc_cookie, lc_allowed) -> None:
    resp = lc_client.get(
        PREFIX + f"/admin/lifecycle/approval-requests/{APPROVAL_ID}", cookies=lc_cookie,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["id"] == APPROVAL_ID
    assert any(
        u == f"{LIFECYCLE_URL}/approval-requests/{APPROVAL_ID}"
        for (_, u, _, _) in lc_allowed.requests
    )


@pytest.mark.parametrize("action,status", [("approve", "APPROVED"), ("reject", "REJECTED")])
def test_decision_forwards_the_caller_and_no_body(
    lc_client, lc_cookie, lc_allowed, action, status,
) -> None:
    """The upstream takes the approver / decider from X-User-Id, so the BFF sends
    no body that could name anyone else — whatever the client posted."""
    target = "tnt_OTHER0000000000000000000001"
    resp = lc_client.post(
        PREFIX + f"/admin/lifecycle/approval-requests/{APPROVAL_ID}/{action}?tenant_id={target}",
        json={"approved_by": "usr_SOMEONE_ELSE"},
        cookies=lc_cookie,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == status
    url = f"{LIFECYCLE_URL}/approval-requests/{APPROVAL_ID}/{action}"
    sent = [kw for (u, kw) in lc_allowed.sent if u == url]
    assert len(sent) == 1
    assert sent[0]["headers"]["X-User-Id"] == CALLER_USER
    assert sent[0]["headers"]["X-Tenant-Id"] == target
    assert "json" not in sent[0] and "content" not in sent[0]
    assert "params" not in sent[0]  # the tenant_id override stays BFF-private


@pytest.mark.parametrize(
    "action,status,code,detail,user_message",
    [
        (
            "approve", 403, "FOUNDATION.LIFECYCLE.POLICY.SELF_APPROVAL_FORBIDDEN",
            "The requester cannot approve their own request; a second person must.",
            "You cannot approve your own request.",
        ),
        (
            "approve", 409, "FOUNDATION.LIFECYCLE.STATE.APPROVAL_NOT_PENDING",
            "The approval request was already decided or has expired.",
            "This request was already decided or has expired.",
        ),
        (
            "approve", 409, "FOUNDATION.LIFECYCLE.STATE.LEGAL_HOLD_ACTIVE",
            "Cannot delete resource with active legal hold.",
            "This resource is under a legal hold.",
        ),
        (
            "reject", 409, "FOUNDATION.LIFECYCLE.STATE.APPROVAL_NOT_PENDING",
            "The approval request was already decided or has expired.",
            "This request was already decided or has expired.",
        ),
    ],
)
def test_decision_refusal_reaches_client_with_its_message(
    lc_client, lc_cookie, monkeypatch, action, status, code, detail, user_message,
) -> None:
    _install(
        FakeLifecycle(responses={f"/{action}": _problem(status, code, detail, user_message)}),
        monkeypatch,
    )
    resp = lc_client.post(
        PREFIX + f"/admin/lifecycle/approval-requests/{APPROVAL_ID}/{action}", cookies=lc_cookie,
    )
    assert resp.status_code == status, resp.text
    body = resp.json()["detail"]
    assert body["user_message"] == user_message
    assert body["detail"]["code"] == code
    assert body["detail"]["detail"] == detail
