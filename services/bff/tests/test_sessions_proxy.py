"""Tests for BFF → orchestrator session proxy endpoints."""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_list_sessions_requires_auth(client: TestClient) -> None:
    resp = client.get("/api/bff/v1/sessions")
    assert resp.status_code == 401


def test_list_sessions_forwards_bearer(
    client: TestClient, authed_cookie: dict[str, str], fake_foundation,
) -> None:
    resp = client.get("/api/bff/v1/sessions", cookies=authed_cookie)
    assert resp.status_code == 200
    # Confirm the BFF forwarded the request to orchestrator (not identity).
    # The fake's catch-all returns {"ok": True}; any orchestrator call
    # would land there if not specifically routed.
    orch_calls = [c for c in fake_foundation.calls if "orch-test" in c[1]]
    assert orch_calls, f"expected orchestrator call, got {fake_foundation.calls}"


def test_create_session_proxies_body(
    client: TestClient, authed_cookie: dict[str, str], fake_foundation,
) -> None:
    resp = client.post(
        "/api/bff/v1/sessions",
        cookies=authed_cookie,
        json={"title": "hello"},
    )
    assert resp.status_code in (200, 201)
    # Body actually forwarded
    posted_body = next(
        (c[2] for c in fake_foundation.calls if c[0] == "POST" and "orch-test" in c[1]),
        None,
    )
    assert posted_body == {"title": "hello"}


def test_cancel_session_proxies_to_orchestrator(
    client: TestClient, authed_cookie: dict[str, str], fake_foundation,
) -> None:
    """BFF cancel endpoint forwards to orchestrator session-level cancel."""
    resp = client.post(
        "/api/bff/v1/sessions/ses_abc/cancel",
        cookies=authed_cookie,
    )
    assert resp.status_code == 200
    # Confirm it hit the orchestrator (orch-test host) and not identity
    cancel_calls = [
        c for c in fake_foundation.calls
        if c[0] == "POST" and "orch-test" in c[1] and "cancel" in c[1]
    ]
    assert cancel_calls, f"expected cancel forwarded to orchestrator, got {fake_foundation.calls}"
    assert "ses_abc" in cancel_calls[0][1]


def test_list_attachments_requires_auth(client: TestClient) -> None:
    resp = client.get("/api/bff/v1/sessions/ses_abc/attachments")
    assert resp.status_code == 401


def test_list_attachments_proxies_to_orchestrator(
    client: TestClient, authed_cookie: dict[str, str], fake_foundation,
) -> None:
    resp = client.get(
        "/api/bff/v1/sessions/ses_abc/attachments",
        cookies=authed_cookie,
    )
    assert resp.status_code == 200
    att_calls = [
        c for c in fake_foundation.calls
        if c[0] == "GET" and "orch-test" in c[1] and "attachments" in c[1]
    ]
    assert att_calls, f"expected attachment list call to orchestrator, got {fake_foundation.calls}"


def test_get_session_requires_auth(client: TestClient) -> None:
    resp = client.get("/api/bff/v1/sessions/ses_abc")
    assert resp.status_code == 401


def test_get_session_proxies_to_orchestrator(
    client: TestClient, authed_cookie: dict[str, str], fake_foundation,
) -> None:
    resp = client.get("/api/bff/v1/sessions/ses_abc", cookies=authed_cookie)
    assert resp.status_code == 200
    orch_calls = [
        c for c in fake_foundation.calls
        if c[0] == "GET" and "orch-test" in c[1] and "ses_abc" in c[1]
    ]
    assert orch_calls, f"expected session GET to orchestrator, got {fake_foundation.calls}"


def test_update_session_requires_auth(client: TestClient) -> None:
    resp = client.patch("/api/bff/v1/sessions/ses_abc", json={"title": "new"})
    assert resp.status_code == 401


def test_update_session_proxies_to_orchestrator(
    client: TestClient, authed_cookie: dict[str, str], fake_foundation,
) -> None:
    resp = client.patch(
        "/api/bff/v1/sessions/ses_abc",
        cookies=authed_cookie,
        json={"title": "Updated Title"},
    )
    assert resp.status_code == 200
    patch_calls = [
        c for c in fake_foundation.calls
        if c[0] == "PATCH" and "orch-test" in c[1] and "ses_abc" in c[1]
    ]
    assert patch_calls, f"expected PATCH to orchestrator, got {fake_foundation.calls}"


def test_delete_session_requires_auth(client: TestClient) -> None:
    resp = client.delete("/api/bff/v1/sessions/ses_abc")
    assert resp.status_code == 401


def test_delete_session_proxies_to_orchestrator(
    client: TestClient, authed_cookie: dict[str, str], fake_foundation,
) -> None:
    resp = client.delete("/api/bff/v1/sessions/ses_abc", cookies=authed_cookie)
    assert resp.status_code == 204


def test_list_messages_requires_auth(client: TestClient) -> None:
    resp = client.get("/api/bff/v1/sessions/ses_abc/messages")
    assert resp.status_code == 401


def test_list_messages_proxies_to_orchestrator(
    client: TestClient, authed_cookie: dict[str, str], fake_foundation,
) -> None:
    resp = client.get(
        "/api/bff/v1/sessions/ses_abc/messages",
        cookies=authed_cookie,
    )
    assert resp.status_code == 200
    msg_calls = [
        c for c in fake_foundation.calls
        if c[0] == "GET" and "orch-test" in c[1] and "messages" in c[1]
    ]
    assert msg_calls, f"expected messages GET to orchestrator, got {fake_foundation.calls}"


def test_list_sessions_does_not_emit_admin_audit(
    client: TestClient, authed_cookie: dict[str, str], fake_foundation,
) -> None:
    """The owner-scoped /sessions list is NOT a privileged action.

    It must not write an ``admin.session.*`` audit event — only the tenant-wide
    replay surface (GET /admin/sessions) does.
    """
    resp = client.get("/api/bff/v1/sessions", cookies=authed_cookie)
    assert resp.status_code == 200
    audit_calls = [
        c for c in fake_foundation.calls
        if c[0] == "POST" and "audit" in c[1] and c[1].endswith("/events")
    ]
    assert not audit_calls, f"owner-scoped list must not audit, got {audit_calls}"


def test_list_messages_does_not_emit_admin_audit(
    client: TestClient, authed_cookie: dict[str, str], fake_foundation,
) -> None:
    """The transcript endpoint is shared by the chat page (a user reading their
    own conversation), so it must not write an ``admin.session.*`` audit event.
    """
    resp = client.get(
        "/api/bff/v1/sessions/ses_abc/messages",
        cookies=authed_cookie,
    )
    assert resp.status_code == 200
    audit_calls = [
        c for c in fake_foundation.calls
        if c[0] == "POST" and "audit" in c[1] and c[1].endswith("/events")
    ]
    assert not audit_calls, f"transcript fetch must not audit, got {audit_calls}"


def test_list_sessions_forwards_no_admin_actor(
    client: TestClient, authed_cookie: dict[str, str], fake_foundation,
) -> None:
    """The normal /sessions proxy must NOT assert X-Actor: ADMIN.

    Without it the orchestrator owner-filters to the caller — the IDOR fix.
    """
    resp = client.get("/api/bff/v1/sessions", cookies=authed_cookie)
    assert resp.status_code == 200
    # We can't see headers via FakeFoundation.calls (only method/url/json),
    # so assert by the route: a normal list never hits authz /check.
    check_calls = [c for c in fake_foundation.calls if c[1].endswith("/check")]
    assert not check_calls, f"normal list must not call authz /check, got {check_calls}"


# ─────────────────────────────────────────────────────────────────────────────
# Admin session-replay endpoint (GET /admin/sessions)
# ─────────────────────────────────────────────────────────────────────────────


def _route_with_authz(fake_foundation, monkeypatch, *, allowed: bool):
    """Patch FakeFoundation so authz /check returns the given allow decision."""
    orig = fake_foundation.route

    def _route(method: str, url: str, **kw):
        if method == "POST" and url.endswith("/check"):
            import httpx as _httpx

            return _httpx.Response(200, json={"allowed": allowed})
        return orig(method, url, **kw)

    monkeypatch.setattr(fake_foundation, "route", _route)


def test_admin_list_sessions_requires_auth(client: TestClient) -> None:
    resp = client.get("/api/bff/v1/admin/sessions")
    assert resp.status_code == 401


def test_admin_list_sessions_forbidden_without_tenant_admin(
    client: TestClient, authed_cookie: dict[str, str], fake_foundation, monkeypatch,
) -> None:
    """A member (authz denies session.delete) gets 403 from the admin endpoint."""
    _route_with_authz(fake_foundation, monkeypatch, allowed=False)
    resp = client.get("/api/bff/v1/admin/sessions", cookies=authed_cookie)
    assert resp.status_code == 403
    # Must NOT have forwarded to the orchestrator.
    orch_calls = [
        c for c in fake_foundation.calls
        if "orch-test" in c[1] and c[1].endswith("/sessions")
    ]
    assert not orch_calls, f"denied admin call must not reach orchestrator, got {orch_calls}"


def test_admin_list_sessions_proxies_when_tenant_admin(
    client: TestClient, authed_cookie: dict[str, str], fake_foundation, monkeypatch,
) -> None:
    """A tenant_admin (authz allows) gets the full-tenant list from orchestrator."""
    _route_with_authz(fake_foundation, monkeypatch, allowed=True)
    resp = client.get("/api/bff/v1/admin/sessions", cookies=authed_cookie)
    assert resp.status_code == 200
    orch_calls = [
        c for c in fake_foundation.calls
        if c[0] == "GET" and "orch-test" in c[1] and c[1].endswith("/sessions")
    ]
    assert orch_calls, f"expected orchestrator session list, got {fake_foundation.calls}"


def test_admin_list_sessions_emits_audit_event(
    client: TestClient, authed_cookie: dict[str, str], fake_foundation, monkeypatch,
) -> None:
    """The admin endpoint reuses the admin.session.list.viewed audit event."""
    _route_with_authz(fake_foundation, monkeypatch, allowed=True)
    resp = client.get("/api/bff/v1/admin/sessions", cookies=authed_cookie)
    assert resp.status_code == 200
    audit_calls = [
        c for c in fake_foundation.calls
        if c[0] == "POST" and "audit" in c[1] and c[1].endswith("/events")
    ]
    assert audit_calls, f"expected audit POST, got {fake_foundation.calls}"
    body = audit_calls[0][2]
    assert body["event_type"] == "admin.session.list.viewed"
    assert body["actor_kind"] == "ADMIN"


def test_admin_list_sessions_survives_audit_failure(
    client: TestClient, authed_cookie: dict[str, str], fake_foundation, monkeypatch,
) -> None:
    """Audit is a side-channel: if the emit raises, /admin/sessions still 200s."""
    _route_with_authz(fake_foundation, monkeypatch, allowed=True)
    orig_route = fake_foundation.route

    def _route(method: str, url: str, **kw):
        if method == "POST" and "audit" in url and url.endswith("/events"):
            raise RuntimeError("audit down")
        return orig_route(method, url, **kw)

    monkeypatch.setattr(fake_foundation, "route", _route)
    resp = client.get("/api/bff/v1/admin/sessions", cookies=authed_cookie)
    assert resp.status_code == 200


def test_upload_attachment_requires_auth(client: TestClient) -> None:
    resp = client.post(
        "/api/bff/v1/sessions/ses_abc/attachments",
        files={"file": ("test.txt", b"hello", "text/plain")},
    )
    assert resp.status_code == 401


def test_upload_attachment_proxies_to_orchestrator(
    client: TestClient, authed_cookie: dict[str, str], fake_foundation,
) -> None:
    resp = client.post(
        "/api/bff/v1/sessions/ses_abc/attachments",
        cookies=authed_cookie,
        files={"file": ("test.txt", b"hello world", "text/plain")},
    )
    assert resp.status_code in (200, 201)
    att_calls = [
        c for c in fake_foundation.calls
        if c[0] == "POST" and "orch-test" in c[1] and "attachments" in c[1]
    ]
    assert att_calls, f"expected attachment POST to orchestrator, got {fake_foundation.calls}"


def test_get_file_content_requires_auth(client: TestClient) -> None:
    resp = client.get("/api/bff/v1/sessions/ses_abc/files/file_01/content")
    assert resp.status_code == 401


def test_get_file_content_proxies_to_orchestrator(
    client: TestClient, authed_cookie: dict[str, str], fake_foundation,
) -> None:
    resp = client.get(
        "/api/bff/v1/sessions/ses_abc/files/file_01/content",
        cookies=authed_cookie,
    )
    assert resp.status_code == 200
    file_calls = [
        c for c in fake_foundation.calls
        if c[0] == "GET" and "orch-test" in c[1] and "content" in c[1]
    ]
    assert file_calls, f"expected file content GET to orchestrator, got {fake_foundation.calls}"


# ─────────────────────────────────────────────────────────────────────────────
# Admin replay of one session (GET /admin/sessions/{id}/runs|messages)
# ─────────────────────────────────────────────────────────────────────────────

_OWNER = "usr_01J5TEST0000000000000OWNER"
_SECRET = "my bank PIN is 4321"


def _admin_replay_routes(
    fake_foundation, monkeypatch, *, allowed: bool = True, status_code: int = 200,
) -> list[tuple[str, str, dict]]:
    """Authz + orchestrator stubs for the replay endpoints; returns the call log."""
    import httpx as _httpx

    calls: list[tuple[str, str, dict]] = []

    def _route(method: str, url: str, **kw):
        calls.append((method, url, kw))
        if method == "POST" and url.endswith("/check"):
            return _httpx.Response(200, json={"allowed": allowed})
        if method == "POST" and url.endswith("/events"):
            return _httpx.Response(201, json={"ok": True})
        if url.endswith("/sessions/ses_abc/messages"):
            if status_code != 200:
                return _httpx.Response(status_code, json={"detail": "not found"})
            return _httpx.Response(200, json={
                "items": [
                    {"id": "msg_1", "role": "user", "content": _SECRET, "sequence": 7},
                    {"id": "msg_2", "role": "assistant", "content": "ok", "sequence": 8},
                ],
                "next_cursor": None,
            })
        if url.endswith("/sessions/ses_abc/runs"):
            return _httpx.Response(200, json={
                "items": [{"id": "run_1", "task_description": _SECRET}],
            })
        if url.endswith("/sessions/ses_abc"):
            return _httpx.Response(200, json={"id": "ses_abc", "user_id": _OWNER})
        return _httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(fake_foundation, "route", _route)
    return calls


def _audit_bodies(calls: list[tuple[str, str, dict]]) -> list[dict]:
    return [kw.get("json") for (m, u, kw) in calls if m == "POST" and u.endswith("/events")]


def test_admin_session_messages_requires_auth(client: TestClient) -> None:
    resp = client.get("/api/bff/v1/admin/sessions/ses_abc/messages")
    assert resp.status_code == 401


def test_admin_session_messages_forbidden_without_tenant_admin(
    client: TestClient, authed_cookie: dict[str, str], fake_foundation, monkeypatch,
) -> None:
    calls = _admin_replay_routes(fake_foundation, monkeypatch, allowed=False)
    resp = client.get("/api/bff/v1/admin/sessions/ses_abc/messages", cookies=authed_cookie)
    assert resp.status_code == 403
    assert not [c for c in calls if "orch-test" in c[1]]
    assert not _audit_bodies(calls)


def test_admin_session_messages_reads_as_admin_with_the_user_params(
    client: TestClient, authed_cookie: dict[str, str], fake_foundation, monkeypatch,
) -> None:
    """The orchestrator's owner check 404s a peer unless X-Actor: ADMIN is sent."""
    calls = _admin_replay_routes(fake_foundation, monkeypatch)
    resp = client.get(
        "/api/bff/v1/admin/sessions/ses_abc/messages?cursor=7&limit=25",
        cookies=authed_cookie,
    )
    assert resp.status_code == 200
    assert [m["id"] for m in resp.json()["items"]] == ["msg_1", "msg_2"]
    reads = [kw for (m, u, kw) in calls if m == "GET" and u.endswith("/ses_abc/messages")]
    assert len(reads) == 1
    assert reads[0]["headers"]["X-Actor"] == "ADMIN"
    assert reads[0]["params"] == {"limit": 25, "cursor": "7"}


def test_admin_session_messages_audits_counts_never_content(
    client: TestClient, authed_cookie: dict[str, str], fake_foundation, monkeypatch,
) -> None:
    import json as _json

    calls = _admin_replay_routes(fake_foundation, monkeypatch)
    resp = client.get("/api/bff/v1/admin/sessions/ses_abc/messages", cookies=authed_cookie)
    assert resp.status_code == 200
    bodies = _audit_bodies(calls)
    assert len(bodies) == 1
    event = bodies[0]
    assert event["event_type"] == "admin.session.messages.viewed"
    assert event["actor_kind"] == "ADMIN"
    assert event["resource_type"] == "session"
    assert event["resource_id"] == "ses_abc"
    assert event["payload"] == {
        "via": "admin_session_view",
        "message_count": 2,
        "subject_user_id": _OWNER,
        "first_sequence": 7,
        "last_sequence": 8,
    }
    assert _SECRET not in _json.dumps(event)


def test_admin_session_messages_failed_read_is_not_audited(
    client: TestClient, authed_cookie: dict[str, str], fake_foundation, monkeypatch,
) -> None:
    calls = _admin_replay_routes(fake_foundation, monkeypatch, status_code=404)
    resp = client.get("/api/bff/v1/admin/sessions/ses_abc/messages", cookies=authed_cookie)
    assert resp.status_code == 404
    assert not _audit_bodies(calls)


def test_admin_session_messages_survive_audit_failure(
    client: TestClient, authed_cookie: dict[str, str], fake_foundation, monkeypatch,
) -> None:
    _admin_replay_routes(fake_foundation, monkeypatch)
    inner = fake_foundation.route

    def _route(method: str, url: str, **kw):
        if method == "POST" and url.endswith("/events"):
            raise RuntimeError("audit down")
        return inner(method, url, **kw)

    monkeypatch.setattr(fake_foundation, "route", _route)
    resp = client.get("/api/bff/v1/admin/sessions/ses_abc/messages", cookies=authed_cookie)
    assert resp.status_code == 200


def test_admin_session_runs_reads_as_admin_and_audits(
    client: TestClient, authed_cookie: dict[str, str], fake_foundation, monkeypatch,
) -> None:
    import json as _json

    calls = _admin_replay_routes(fake_foundation, monkeypatch)
    resp = client.get("/api/bff/v1/admin/sessions/ses_abc/runs", cookies=authed_cookie)
    assert resp.status_code == 200
    reads = [kw for (m, u, kw) in calls if m == "GET" and u.endswith("/ses_abc/runs")]
    assert reads and reads[0]["headers"]["X-Actor"] == "ADMIN"
    bodies = _audit_bodies(calls)
    assert len(bodies) == 1
    assert bodies[0]["event_type"] == "admin.session.runs.viewed"
    assert bodies[0]["payload"] == {
        "via": "admin_session_view", "run_count": 1, "subject_user_id": _OWNER,
    }
    assert _SECRET not in _json.dumps(bodies[0])


def test_admin_session_runs_forbidden_without_tenant_admin(
    client: TestClient, authed_cookie: dict[str, str], fake_foundation, monkeypatch,
) -> None:
    calls = _admin_replay_routes(fake_foundation, monkeypatch, allowed=False)
    resp = client.get("/api/bff/v1/admin/sessions/ses_abc/runs", cookies=authed_cookie)
    assert resp.status_code == 403
    assert not [c for c in calls if "orch-test" in c[1]]
