"""Tests for BFF → orchestrator workspace proxy endpoints.

Mirrors ``test_sessions_proxy.py``: the FakeFoundation stub's catch-all
returns ``{"ok": True}`` for any orchestrator route not explicitly matched, so
these tests assert the BFF forwarded to the orchestrator host (``orch-test``)
with the right method/path rather than exercising real orchestrator logic.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient


def test_list_workspaces_requires_auth(client: TestClient) -> None:
    resp = client.get("/api/bff/v1/workspaces")
    assert resp.status_code == 401


def test_list_workspaces_proxies_to_orchestrator(
    client: TestClient, authed_cookie: dict[str, str], fake_foundation,
) -> None:
    resp = client.get("/api/bff/v1/workspaces", cookies=authed_cookie)
    assert resp.status_code == 200
    ws_calls = [
        c for c in fake_foundation.calls
        if c[0] == "GET" and "orch-test" in c[1] and c[1].endswith("/workspaces")
    ]
    assert ws_calls, f"expected workspaces GET to orchestrator, got {fake_foundation.calls}"


def test_create_workspace_proxies_body(
    client: TestClient, authed_cookie: dict[str, str], fake_foundation,
) -> None:
    resp = client.post(
        "/api/bff/v1/workspaces",
        cookies=authed_cookie,
        json={"name": "Research", "skills": ["docx"]},
    )
    assert resp.status_code in (200, 201)
    posted = next(
        (
            c[2] for c in fake_foundation.calls
            if c[0] == "POST" and "orch-test" in c[1] and c[1].endswith("/workspaces")
        ),
        None,
    )
    assert posted == {"name": "Research", "skills": ["docx"]}


def test_upload_workspace_file_proxies_to_orchestrator(
    client: TestClient, authed_cookie: dict[str, str], fake_foundation,
) -> None:
    resp = client.post(
        "/api/bff/v1/workspaces/wks_abc/files",
        cookies=authed_cookie,
        files={"file": ("notes.txt", b"hello world", "text/plain")},
        data={"description": "some notes"},
    )
    assert resp.status_code in (200, 201)
    file_calls = [
        c for c in fake_foundation.calls
        if c[0] == "POST" and "orch-test" in c[1] and "wks_abc/files" in c[1]
    ]
    assert file_calls, f"expected file upload POST to orchestrator, got {fake_foundation.calls}"


def test_get_workspace_error_passes_through(
    client: TestClient, authed_cookie: dict[str, str], fake_foundation, monkeypatch,
) -> None:
    """A 404 (+ problem+json body) from the orchestrator is forwarded verbatim."""
    orig = fake_foundation.route

    def _route(method: str, url: str, **kw):
        if method == "GET" and url.endswith("/workspaces/wks_missing"):
            return httpx.Response(
                404,
                json={
                    "title": "Not Found",
                    "status": 404,
                    "user_message": "工作区不存在",
                },
            )
        return orig(method, url, **kw)

    monkeypatch.setattr(fake_foundation, "route", _route)

    resp = client.get("/api/bff/v1/workspaces/wks_missing", cookies=authed_cookie)
    assert resp.status_code == 404
    assert resp.json().get("user_message") == "工作区不存在"


# ─────────────────────────────────────────────────────────────────────────────
# Clearance forwarding (X-User-Clearance) on the write endpoints
# ─────────────────────────────────────────────────────────────────────────────

# ``authed_cookie_with_clearance`` is shared from ``conftest.py`` (bakes a
# ``clearance: "L3"`` claim into the signed session cookie so the BFF
# middleware populates the ``current_clearance`` contextvar non-empty).


def _capture_headers(fake_foundation, monkeypatch) -> list[dict[str, str]]:
    """Patch ``fake_foundation.route`` to also record forwarded headers.

    The shared ``FakeFoundation.calls`` tuple only retains ``(method, url,
    json)`` (see ``conftest.py``); the clearance header lives in
    ``kwargs["headers"]``, so we monkeypatch ``route`` to capture it into a
    fresh list without disturbing the shared call log used by other tests.
    """
    captured: list[dict[str, str]] = []
    orig = fake_foundation.route

    def _route(method: str, url: str, **kw):
        captured.append(dict(kw.get("headers") or {}))
        return orig(method, url, **kw)

    monkeypatch.setattr(fake_foundation, "route", _route)
    return captured


def test_create_workspace_forwards_clearance(
    client: TestClient, authed_cookie_with_clearance: dict[str, str],
    fake_foundation, monkeypatch,
) -> None:
    """create_workspace forwards X-User-Clearance when the cookie carries one."""
    captured = _capture_headers(fake_foundation, monkeypatch)
    resp = client.post(
        "/api/bff/v1/workspaces",
        cookies=authed_cookie_with_clearance,
        json={"name": "Secret"},
    )
    assert resp.status_code in (200, 201)
    assert any(h.get("X-User-Clearance") == "L3" for h in captured), (
        f"expected X-User-Clearance: L3 forwarded, got headers={captured}"
    )


def test_update_workspace_forwards_clearance(
    client: TestClient, authed_cookie_with_clearance: dict[str, str],
    fake_foundation, monkeypatch,
) -> None:
    """update_workspace forwards X-User-Clearance when the cookie carries one."""
    captured = _capture_headers(fake_foundation, monkeypatch)
    resp = client.patch(
        "/api/bff/v1/workspaces/wks_abc",
        cookies=authed_cookie_with_clearance,
        json={"name": "Renamed"},
    )
    assert resp.status_code in (200, 204)
    # foundation_headers always sets X-Tenant-Id / X-User-Id on the
    # orchestrator-bound call; pick that one out (vs. any incidental call).
    orch_headers = [h for h in captured if "X-Tenant-Id" in h]
    assert orch_headers, f"expected orchestrator-bound PATCH, got {captured}"
    assert orch_headers[0].get("X-User-Clearance") == "L3", (
        f"expected X-User-Clearance: L3 forwarded, got {orch_headers[0]}"
    )


def test_upload_workspace_file_forwards_clearance(
    client: TestClient, authed_cookie_with_clearance: dict[str, str],
    fake_foundation, monkeypatch,
) -> None:
    """upload_workspace_file forwards X-User-Clearance on the multipart POST."""
    captured = _capture_headers(fake_foundation, monkeypatch)
    resp = client.post(
        "/api/bff/v1/workspaces/wks_abc/files",
        cookies=authed_cookie_with_clearance,
        files={"file": ("notes.txt", b"hello world", "text/plain")},
        data={"description": "some notes"},
    )
    assert resp.status_code in (200, 201)
    assert any(h.get("X-User-Clearance") == "L3" for h in captured), (
        f"expected X-User-Clearance: L3 forwarded, got headers={captured}"
    )


def test_upload_workspace_file_forwards_sensitivity_level(
    client: TestClient, authed_cookie_with_clearance: dict[str, str],
    fake_foundation, monkeypatch,
) -> None:
    """upload_workspace_file forwards the ``sensitivity_level`` form field.

    Regression guard: the BFF must not silently drop ``sensitivity_level`` from
    the multipart upload (otherwise every file lands as L2 regardless of caller
    intent, defeating the per-file sensitivity feature).
    """
    captured_data: list[dict | None] = []
    orig = fake_foundation.route

    def _route(method: str, url: str, **kw):
        captured_data.append(kw.get("data"))
        return orig(method, url, **kw)

    monkeypatch.setattr(fake_foundation, "route", _route)

    resp = client.post(
        "/api/bff/v1/workspaces/wks_abc/files",
        cookies=authed_cookie_with_clearance,
        files={"file": ("notes.txt", b"hello world", "text/plain")},
        data={"description": "some notes", "sensitivity_level": "L4"},
    )
    assert resp.status_code in (200, 201)
    assert captured_data, "expected an orchestrator-bound upload call"
    forwarded = captured_data[0] or {}
    assert forwarded.get("sensitivity_level") == "L4", (
        f"expected sensitivity_level=L4 forwarded in multipart form, got {forwarded}"
    )


def test_create_workspace_omits_clearance_when_cookie_lacks_claim(
    client: TestClient, authed_cookie: dict[str, str],
    fake_foundation, monkeypatch,
) -> None:
    """No clearance claim -> no X-User-Clearance header (service-token parity).

    The base ``authed_cookie`` fixture bakes no ``clearance`` field, so the
    contextvar resolves to "" and ``foundation_headers`` omits the header
    entirely (matching the service-token path).  Guards against the BFF
    inventing a default level on the caller's behalf.
    """
    captured = _capture_headers(fake_foundation, monkeypatch)
    resp = client.post(
        "/api/bff/v1/workspaces",
        cookies=authed_cookie,
        json={"name": "Plain"},
    )
    assert resp.status_code in (200, 201)
    assert captured, "expected at least one forwarded call"
    assert all("X-User-Clearance" not in h for h in captured), (
        f"X-User-Clearance must not be sent without a claim, got {captured}"
    )


def test_workspace_403_passes_through(
    client: TestClient, authed_cookie: dict[str, str], fake_foundation, monkeypatch,
) -> None:
    """A 403 (+ problem+json body) from the orchestrator is forwarded verbatim.

    Mirrors ``test_get_workspace_error_passes_through``: the orchestrator's
    problem+json (with ``user_message``) reaches the frontend intact so the
    BFF never rewrites a clearance-denied 403 into something else.
    """
    orig = fake_foundation.route

    def _route(method: str, url: str, **kw):
        if method == "POST" and url.endswith("/workspaces"):
            return httpx.Response(
                403,
                json={
                    "title": "Forbidden",
                    "status": 403,
                    "user_message": "密级不足，无法创建该工作区",
                },
            )
        return orig(method, url, **kw)

    monkeypatch.setattr(fake_foundation, "route", _route)

    resp = client.post(
        "/api/bff/v1/workspaces",
        cookies=authed_cookie,
        json={"name": "Denied"},
    )
    assert resp.status_code == 403
    assert resp.json().get("user_message") == "密级不足，无法创建该工作区"
