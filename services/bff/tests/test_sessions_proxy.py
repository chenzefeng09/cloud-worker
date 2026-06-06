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
