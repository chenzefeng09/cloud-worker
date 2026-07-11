"""HTTP-layer tests for knowledge-ingest."""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_healthz(app_client: TestClient) -> None:
    resp = app_client.get("/api/knowledge/v1/healthz")
    assert resp.status_code == 200
    assert resp.json()["service"] == "knowledge-ingest"


def test_upload_requires_tenant(app_client: TestClient) -> None:
    # authz guard fires before the current_tenant dependency: missing tenant
    # is caught at the guard level (403) before the 401 dependency runs.
    resp = app_client.post(
        "/api/knowledge/v1/documents",
        data={"title": "t", "mime": "text/plain"},
        files={"file": ("x.txt", b"hello", "text/plain")},
    )
    assert resp.status_code in (401, 403)


def test_upload_and_get(app_client: TestClient, tenant_headers: dict[str, str]) -> None:
    resp = app_client.post(
        "/api/knowledge/v1/documents",
        headers=tenant_headers,
        data={"title": "hello.txt", "mime": "text/plain"},
        files={"file": ("hello.txt", b"hello world", "text/plain")},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "PENDING"

    detail = app_client.get(
        f"/api/knowledge/v1/documents/{body['id']}", headers=tenant_headers,
    ).json()
    assert detail["chunk_count"] == 0


def test_upload_unsupported_mime(
    app_client: TestClient, tenant_headers: dict[str, str],
) -> None:
    resp = app_client.post(
        "/api/knowledge/v1/documents",
        headers=tenant_headers,
        data={"title": "x", "mime": "image/png"},
        files={"file": ("x.png", b"\x89PNG", "image/png")},
    )
    assert resp.status_code == 415


def test_ingest_creates_chunks(
    app_client: TestClient, tenant_headers: dict[str, str],
) -> None:
    body = ("alpha beta gamma " * 50).encode()
    created = app_client.post(
        "/api/knowledge/v1/documents",
        headers=tenant_headers,
        data={"title": "doc", "mime": "text/plain"},
        files={"file": ("doc.txt", body, "text/plain")},
    ).json()

    ingest = app_client.post(
        f"/api/knowledge/v1/documents/{created['id']}/ingest",
        headers=tenant_headers,
    )
    assert ingest.status_code == 200, ingest.text
    payload = ingest.json()
    assert payload["status"] == "READY"
    assert payload["chunks_created"] > 1


def test_ingest_unknown_returns_404(
    app_client: TestClient, tenant_headers: dict[str, str],
) -> None:
    resp = app_client.post(
        "/api/knowledge/v1/documents/doc_missing/ingest", headers=tenant_headers,
    )
    assert resp.status_code == 404


def test_delete_document(app_client: TestClient, tenant_headers: dict[str, str]) -> None:
    created = app_client.post(
        "/api/knowledge/v1/documents",
        headers=tenant_headers,
        data={"title": "doc", "mime": "text/plain"},
        files={"file": ("doc.txt", b"hello world", "text/plain")},
    ).json()

    resp = app_client.delete(
        f"/api/knowledge/v1/documents/{created['id']}", headers=tenant_headers,
    )
    assert resp.status_code == 204, resp.text

    # Document no longer fetchable.
    detail = app_client.get(
        f"/api/knowledge/v1/documents/{created['id']}", headers=tenant_headers,
    )
    assert detail.status_code == 404


def test_delete_unknown_document_returns_404(
    app_client: TestClient, tenant_headers: dict[str, str],
) -> None:
    resp = app_client.delete(
        "/api/knowledge/v1/documents/doc_missing", headers=tenant_headers,
    )
    assert resp.status_code == 404


def test_search_returns_hits(
    app_client: TestClient, tenant_headers: dict[str, str],
) -> None:
    body = ("alpha beta gamma " * 50).encode()
    created = app_client.post(
        "/api/knowledge/v1/documents",
        headers=tenant_headers,
        data={"title": "doc", "mime": "text/plain"},
        files={"file": ("doc.txt", body, "text/plain")},
    ).json()
    app_client.post(
        f"/api/knowledge/v1/documents/{created['id']}/ingest",
        headers=tenant_headers,
    )

    resp = app_client.get(
        "/api/knowledge/v1/search",
        params={"q": "alpha beta gamma", "k": 3},
        headers=tenant_headers,
    )
    assert resp.status_code == 200
    hits = resp.json()["hits"]
    assert 1 <= len(hits) <= 3
    assert all("score" in h for h in hits)


def test_upload_persists_origin_session_id(
    app_client: TestClient, tenant_headers: dict[str, str],
) -> None:
    resp = app_client.post(
        "/api/knowledge/v1/documents",
        headers=tenant_headers,
        data={
            "title": "[memory] hi",
            "mime": "text/plain",
            "origin_session_id": "ses_upload1",
        },
        files={"file": ("memory.txt", b"hello world", "text/plain")},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["origin_session_id"] == "ses_upload1"

    detail = app_client.get(
        f"/api/knowledge/v1/documents/{body['id']}", headers=tenant_headers,
    ).json()
    assert detail["origin_session_id"] == "ses_upload1"


def test_upload_without_origin_session_id_defaults_null(
    app_client: TestClient, tenant_headers: dict[str, str],
) -> None:
    resp = app_client.post(
        "/api/knowledge/v1/documents",
        headers=tenant_headers,
        data={"title": "plain", "mime": "text/plain"},
        files={"file": ("plain.txt", b"hello", "text/plain")},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["origin_session_id"] is None


def test_search_excludes_origin_session_via_query(
    app_client: TestClient, tenant_headers: dict[str, str],
) -> None:
    marker = ("uniquely-identifiable-marker " * 8).encode()
    own = app_client.post(
        "/api/knowledge/v1/documents",
        headers=tenant_headers,
        data={"title": "[memory] own", "mime": "text/plain",
              "origin_session_id": "ses_self"},
        files={"file": ("own.txt", marker, "text/plain")},
    ).json()
    app_client.post(
        f"/api/knowledge/v1/documents/{own['id']}/ingest", headers=tenant_headers,
    )

    resp = app_client.get(
        "/api/knowledge/v1/search",
        params={"q": "uniquely-identifiable-marker", "k": 10,
                "exclude_origin_session_id": "ses_self"},
        headers=tenant_headers,
    )
    assert resp.status_code == 200, resp.text
    ids = {h["document_id"] for h in resp.json()["hits"]}
    assert own["id"] not in ids


def test_search_empty_query_returns_no_hits(
    app_client: TestClient, tenant_headers: dict[str, str],
) -> None:
    resp = app_client.get(
        "/api/knowledge/v1/search",
        params={"q": "   "},
        headers=tenant_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["hits"] == []


def test_list_documents_tenant_isolated(
    app_client: TestClient,
    tenant_headers: dict[str, str],
    other_tenant_headers: dict[str, str],
) -> None:
    app_client.post(
        "/api/knowledge/v1/documents",
        headers=tenant_headers,
        data={"title": "mine", "mime": "text/plain"},
        files={"file": ("x.txt", b"x", "text/plain")},
    )
    other = app_client.get(
        "/api/knowledge/v1/documents", headers=other_tenant_headers,
    ).json()
    assert other["total"] == 0
