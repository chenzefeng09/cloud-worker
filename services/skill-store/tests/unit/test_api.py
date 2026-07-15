"""HTTP-layer tests for skill-store."""

from __future__ import annotations

import hashlib
import io
import tarfile

from fastapi.testclient import TestClient

VALID_FRONTMATTER = "---\nname: poet\ndescription: writes poetry\n---\n# Poet\nbody\n"


def _make_tarball(
    members: dict[str, bytes],
    *,
    add_symlink: tuple[str, str] | None = None,
) -> bytes:
    """Build a gzip tar from ``{archive_path: content_bytes}``.

    ``add_symlink=(linkname, target)`` injects a symlink member (used to test
    the symlink-rejection path).
    """
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for path, content in members.items():
            info = tarfile.TarInfo(name=path)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
        if add_symlink is not None:
            linkname, target = add_symlink
            link = tarfile.TarInfo(name=linkname)
            link.type = tarfile.SYMTYPE
            link.linkname = target
            tar.addfile(link)
    return buf.getvalue()


def _valid_skill_tarball(*, subdir: str | None = None) -> bytes:
    """A minimal valid skill package (root or single-subdir layout)."""
    prefix = f"{subdir}/" if subdir else ""
    return _make_tarball(
        {
            f"{prefix}SKILL.md": VALID_FRONTMATTER.encode("utf-8"),
            f"{prefix}references/notes.md": b"see also",
        }
    )


def _create_skill(
    client: TestClient,
    headers: dict[str, str],
    name: str = "poet",
    visibility: str = "tenant",
) -> dict:
    # Default to "tenant" so the skill appears on the shelf (browse list);
    # pass visibility="private" explicitly to test shelf exclusion.
    resp = client.post(
        "/api/skills/v1/skills",
        headers=headers,
        json={"name": name, "description": "demo", "visibility": visibility},
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


def test_private_skill_excluded_from_shelf(
    app_client: TestClient, tenant_headers: dict[str, str],
) -> None:
    _create_skill(app_client, tenant_headers, name="hidden", visibility="private")
    listing = app_client.get("/api/skills/v1/skills", headers=tenant_headers).json()
    assert listing["total"] == 0


def test_install_uninstall_roundtrip(
    app_client: TestClient, tenant_headers: dict[str, str],
) -> None:
    skill = _create_skill(app_client, tenant_headers, visibility="private")

    # Install (even a private skill the user can reach in their tenant).
    resp = app_client.post(
        f"/api/skills/v1/skills/{skill['id']}/install",
        headers=tenant_headers,
        json={"enabled_by_default": True},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["enabled_by_default"] is True

    installed = app_client.get(
        "/api/skills/v1/skills/installed", headers=tenant_headers,
    ).json()
    assert installed["total"] == 1
    assert installed["items"][0]["id"] == skill["id"]
    assert installed["items"][0]["enabled_by_default"] is True

    # Re-install with a different default updates in place (idempotent).
    resp = app_client.post(
        f"/api/skills/v1/skills/{skill['id']}/install",
        headers=tenant_headers,
        json={"enabled_by_default": False},
    )
    assert resp.status_code == 201
    installed = app_client.get(
        "/api/skills/v1/skills/installed", headers=tenant_headers,
    ).json()
    assert installed["total"] == 1
    assert installed["items"][0]["enabled_by_default"] is False

    # Uninstall.
    resp = app_client.delete(
        f"/api/skills/v1/skills/{skill['id']}/install", headers=tenant_headers,
    )
    assert resp.status_code == 204
    installed = app_client.get(
        "/api/skills/v1/skills/installed", headers=tenant_headers,
    ).json()
    assert installed["total"] == 0


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


def _upload(
    client: TestClient,
    headers: dict[str, str],
    skill_id: str,
    *,
    version: str = "1.0.0",
    tarball: bytes,
    expected_sha256: str | None = None,
) -> object:
    data: dict[str, str] = {"version": version}
    # Pass a (now-ignored) uploader manifest to prove the server overrides it.
    data["manifest"] = '{"attacker": "controlled"}'
    if expected_sha256 is not None:
        data["expected_sha256"] = expected_sha256
    return client.post(
        f"/api/skills/v1/skills/{skill_id}/versions",
        headers=headers,
        data=data,
        files={"file": ("skill.tar.gz", tarball, "application/gzip")},
    )


def test_upload_version(app_client: TestClient, tenant_headers: dict[str, str]) -> None:
    skill = _create_skill(app_client, tenant_headers)
    tarball = _valid_skill_tarball()
    expected = hashlib.sha256(tarball).hexdigest()
    resp = _upload(
        app_client, tenant_headers, skill["id"], tarball=tarball, expected_sha256=expected,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["sha256"] == expected
    # Manifest is server-authoritative, derived from SKILL.md — NOT the
    # uploader-supplied manifest form value.
    assert body["manifest"] == {
        "name": "poet",
        "description": "writes poetry",
        "skill_md_path": "SKILL.md",
        "format": "skill_md_v1",
        "sha256": expected,
    }


def test_upload_version_subdir_layout(
    app_client: TestClient, tenant_headers: dict[str, str],
) -> None:
    skill = _create_skill(app_client, tenant_headers)
    tarball = _valid_skill_tarball(subdir="poet")
    resp = _upload(app_client, tenant_headers, skill["id"], tarball=tarball)
    assert resp.status_code == 201, resp.text
    assert resp.json()["manifest"]["skill_md_path"] == "poet/SKILL.md"


def test_upload_version_sha_mismatch(
    app_client: TestClient, tenant_headers: dict[str, str],
) -> None:
    skill = _create_skill(app_client, tenant_headers)
    resp = _upload(
        app_client, tenant_headers, skill["id"],
        tarball=_valid_skill_tarball(), expected_sha256="deadbeef",
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "SKILL.SHA256_MISMATCH"


def _assert_invalid(resp: object, *, contains: str) -> None:
    assert resp.status_code == 400, resp.text  # type: ignore[attr-defined]
    body = resp.json()  # type: ignore[attr-defined]
    assert body["code"] == "SKILL.PACKAGE.INVALID"
    assert contains in body["user_message"]


def test_upload_rejects_non_gzip(
    app_client: TestClient, tenant_headers: dict[str, str],
) -> None:
    skill = _create_skill(app_client, tenant_headers)
    resp = _upload(app_client, tenant_headers, skill["id"], tarball=b"not a tarball")
    _assert_invalid(resp, contains="gzip")


def test_upload_rejects_missing_skill_md(
    app_client: TestClient, tenant_headers: dict[str, str],
) -> None:
    skill = _create_skill(app_client, tenant_headers)
    tarball = _make_tarball({"README.md": b"no skill md here"})
    resp = _upload(app_client, tenant_headers, skill["id"], tarball=tarball)
    _assert_invalid(resp, contains="缺少 SKILL.md")


def test_upload_rejects_multiple_skill_md(
    app_client: TestClient, tenant_headers: dict[str, str],
) -> None:
    skill = _create_skill(app_client, tenant_headers)
    tarball = _make_tarball(
        {
            "SKILL.md": VALID_FRONTMATTER.encode("utf-8"),
            "other/SKILL.md": VALID_FRONTMATTER.encode("utf-8"),
        }
    )
    resp = _upload(app_client, tenant_headers, skill["id"], tarball=tarball)
    _assert_invalid(resp, contains="多个 SKILL.md")


def test_upload_rejects_missing_name(
    app_client: TestClient, tenant_headers: dict[str, str],
) -> None:
    skill = _create_skill(app_client, tenant_headers)
    fm = "---\ndescription: only a description\n---\nbody\n"
    tarball = _make_tarball({"SKILL.md": fm.encode("utf-8")})
    resp = _upload(app_client, tenant_headers, skill["id"], tarball=tarball)
    _assert_invalid(resp, contains="name")


def test_upload_rejects_missing_description(
    app_client: TestClient, tenant_headers: dict[str, str],
) -> None:
    skill = _create_skill(app_client, tenant_headers)
    fm = "---\nname: poet\n---\nbody\n"
    tarball = _make_tarball({"SKILL.md": fm.encode("utf-8")})
    resp = _upload(app_client, tenant_headers, skill["id"], tarball=tarball)
    _assert_invalid(resp, contains="description")


def test_upload_rejects_malformed_yaml(
    app_client: TestClient, tenant_headers: dict[str, str],
) -> None:
    skill = _create_skill(app_client, tenant_headers)
    fm = "---\nname: [unclosed\ndescription: x\n---\nbody\n"
    tarball = _make_tarball({"SKILL.md": fm.encode("utf-8")})
    resp = _upload(app_client, tenant_headers, skill["id"], tarball=tarball)
    _assert_invalid(resp, contains="frontmatter 格式错误")


def test_upload_rejects_description_over_limit(
    app_client: TestClient, tenant_headers: dict[str, str],
) -> None:
    skill = _create_skill(app_client, tenant_headers)
    # Ceiling is MAX_DESCRIPTION_LEN (2000, aligned with the varchar column);
    # 2001 chars must still be rejected as over-limit.
    long_desc = "x" * 2001
    fm = f"---\nname: too-long-desc\ndescription: {long_desc}\n---\nbody\n"
    tarball = _make_tarball({"SKILL.md": fm.encode("utf-8")})
    resp = _upload(app_client, tenant_headers, skill["id"], tarball=tarball)
    _assert_invalid(resp, contains="description 过长")


def test_upload_accepts_description_at_2000_character_limit(
    app_client: TestClient, tenant_headers: dict[str, str],
) -> None:
    skill = _create_skill(app_client, tenant_headers)
    description = "x" * 2000
    fm = f"---\nname: max-description\ndescription: {description}\n---\nbody\n"
    tarball = _make_tarball({"SKILL.md": fm.encode("utf-8")})

    resp = _upload(app_client, tenant_headers, skill["id"], tarball=tarball)

    assert resp.status_code == 201, resp.text
    assert resp.json()["manifest"]["description"] == description


def test_upload_rejects_path_traversal(
    app_client: TestClient, tenant_headers: dict[str, str],
) -> None:
    skill = _create_skill(app_client, tenant_headers)
    tarball = _make_tarball(
        {
            "SKILL.md": VALID_FRONTMATTER.encode("utf-8"),
            "../evil.sh": b"rm -rf /",
        }
    )
    resp = _upload(app_client, tenant_headers, skill["id"], tarball=tarball)
    _assert_invalid(resp, contains="..")


def test_upload_rejects_symlink(
    app_client: TestClient, tenant_headers: dict[str, str],
) -> None:
    skill = _create_skill(app_client, tenant_headers)
    tarball = _make_tarball(
        {"SKILL.md": VALID_FRONTMATTER.encode("utf-8")},
        add_symlink=("evil", "/etc/passwd"),
    )
    resp = _upload(app_client, tenant_headers, skill["id"], tarball=tarball)
    _assert_invalid(resp, contains="符号链接")


def test_upload_rejects_too_many_members(
    app_client: TestClient, tenant_headers: dict[str, str],
) -> None:
    skill = _create_skill(app_client, tenant_headers)
    members = {"SKILL.md": VALID_FRONTMATTER.encode("utf-8")}
    for i in range(2001):
        members[f"f{i}.txt"] = b"x"
    tarball = _make_tarball(members)
    resp = _upload(app_client, tenant_headers, skill["id"], tarball=tarball)
    _assert_invalid(resp, contains="成员数过多")


def test_upload_rejects_total_size_bomb(
    app_client: TestClient, tenant_headers: dict[str, str],
) -> None:
    skill = _create_skill(app_client, tenant_headers)
    # 12 members of ~5 MiB each => > 50 MiB decompressed, but compresses tiny.
    chunk = b"\0" * (5 * 1024 * 1024)
    members = {"SKILL.md": VALID_FRONTMATTER.encode("utf-8")}
    for i in range(12):
        members[f"big{i}.bin"] = chunk
    tarball = _make_tarball(members)
    resp = _upload(app_client, tenant_headers, skill["id"], tarball=tarball)
    _assert_invalid(resp, contains="总大小")


def test_download_redirects_to_presigned(
    app_client: TestClient, tenant_headers: dict[str, str],
) -> None:
    skill = _create_skill(app_client, tenant_headers)
    _upload(app_client, tenant_headers, skill["id"], tarball=_valid_skill_tarball())
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
    _upload(app_client, tenant_headers, skill["id"], tarball=_valid_skill_tarball())
    assert blob_store.size == 1
    resp = app_client.delete(
        f"/api/skills/v1/skills/{skill['id']}/versions/1.0.0",
        headers=tenant_headers,
    )
    assert resp.status_code == 204
    assert blob_store.size == 0


def test_delete_skill(
    app_client: TestClient, tenant_headers: dict[str, str], blob_store,
) -> None:
    skill = _create_skill(app_client, tenant_headers)
    for ver in ("1.0.0", "2.0.0"):
        _upload(
            app_client, tenant_headers, skill["id"],
            version=ver, tarball=_valid_skill_tarball(),
        )
    assert blob_store.size == 2

    resp = app_client.delete(
        f"/api/skills/v1/skills/{skill['id']}", headers=tenant_headers,
    )
    assert resp.status_code == 204
    # All version blobs cleaned up.
    assert blob_store.size == 0
    # Skill no longer fetchable nor on the shelf.
    assert app_client.get(
        f"/api/skills/v1/skills/{skill['id']}", headers=tenant_headers,
    ).status_code == 404
    listing = app_client.get(
        "/api/skills/v1/skills", headers=tenant_headers,
    ).json()
    assert listing["total"] == 0


def test_delete_skill_404(
    app_client: TestClient, tenant_headers: dict[str, str],
) -> None:
    resp = app_client.delete(
        "/api/skills/v1/skills/skl_unknown", headers=tenant_headers,
    )
    assert resp.status_code == 404


def test_list_versions(app_client: TestClient, tenant_headers: dict[str, str]) -> None:
    skill = _create_skill(app_client, tenant_headers)
    for ver in ("1.0.0", "2.0.0"):
        _upload(
            app_client, tenant_headers, skill["id"],
            version=ver, tarball=_valid_skill_tarball(),
        )
    resp = app_client.get(
        f"/api/skills/v1/skills/{skill['id']}/versions", headers=tenant_headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 2
    assert {v["version"] for v in body["items"]} == {"1.0.0", "2.0.0"}


def test_list_versions_unknown_skill_404(
    app_client: TestClient, tenant_headers: dict[str, str],
) -> None:
    resp = app_client.get(
        "/api/skills/v1/skills/skl_unknown/versions", headers=tenant_headers,
    )
    assert resp.status_code == 404


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
        headers={
            "X-Tenant-Id": "tnt_01J5TEST00000000000000TNT1",
            "X-Service-Scopes": "mcp.read_internal,skill.read_internal",
        },
    )
    assert resp.status_code == 200, resp.text


def test_internal_endpoint_rejects_query_tenant_mismatch(
    app_client: TestClient,
) -> None:
    resp = app_client.get(
        "/api/skills/v1/internal/skills",
        params={"tenant_id": "tnt_01J5TEST00000000000000TNT2"},
        headers={
            "X-Tenant-Id": "tnt_01J5TEST00000000000000TNT1",
            "X-Service-Scopes": "skill.read_internal",
        },
    )
    assert resp.status_code == 403


def test_install_with_version_field(
    app_client: TestClient, tenant_headers: dict[str, str],
) -> None:
    skill = _create_skill(app_client, tenant_headers, visibility="private")
    _upload(app_client, tenant_headers, skill["id"], tarball=_valid_skill_tarball())

    resp = app_client.post(
        f"/api/skills/v1/skills/{skill['id']}/install",
        headers=tenant_headers,
        json={"enabled_by_default": True, "version": "1.0.0"},
    )
    assert resp.status_code == 201, resp.text


def test_install_with_unknown_version_404(
    app_client: TestClient, tenant_headers: dict[str, str],
) -> None:
    skill = _create_skill(app_client, tenant_headers, visibility="private")
    resp = app_client.post(
        f"/api/skills/v1/skills/{skill['id']}/install",
        headers=tenant_headers,
        json={"enabled_by_default": True, "version": "9.9.9"},
    )
    assert resp.status_code == 404
    assert resp.json()["code"] == "SKILL.VERSION.NOT_FOUND"


def test_internal_resolve_requires_scope(app_client: TestClient) -> None:
    resp = app_client.get(
        "/api/skills/v1/internal/skills/resolve",
        params={
            "tenant_id": "tnt_01J5TEST00000000000000TNT1",
            "user_id": "usr_01J5TEST0000000000000USER1",
            "names": "poet",
        },
    )
    assert resp.status_code == 403


def test_internal_resolve_empty_names(
    app_client: TestClient, internal_headers: dict[str, str],
) -> None:
    resp = app_client.get(
        "/api/skills/v1/internal/skills/resolve",
        params={
            "tenant_id": "tnt_01J5TEST00000000000000TNT1",
            "user_id": "usr_01J5TEST0000000000000USER1",
        },
        headers=internal_headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"items": [], "total": 0}


def test_internal_resolve_returns_presigned(
    app_client: TestClient,
    tenant_headers: dict[str, str],
    internal_headers: dict[str, str],
) -> None:
    # Set up: create skill, upload v1.0.0, install pinning that version.
    skill = _create_skill(app_client, tenant_headers, visibility="private")
    _upload(app_client, tenant_headers, skill["id"], tarball=_valid_skill_tarball())
    install_resp = app_client.post(
        f"/api/skills/v1/skills/{skill['id']}/install",
        headers=tenant_headers,
        json={"enabled_by_default": True, "version": "1.0.0"},
    )
    assert install_resp.status_code == 201, install_resp.text

    resp = app_client.get(
        "/api/skills/v1/internal/skills/resolve",
        params={
            "tenant_id": "tnt_01J5TEST00000000000000TNT1",
            "user_id": "usr_01J5TEST0000000000000USER1",
            "names": "poet,ghost",
        },
        headers=internal_headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # ``ghost`` was not installed -> not in the resolved list.
    assert body["total"] == 1
    item = body["items"][0]
    assert item["name"] == "poet"
    assert item["version"] == "1.0.0"
    assert item["sha256"]
    # InMemoryBlobStore presign returns ``memory://...?ttl=...``; in production
    # this is a real HTTP presigned URL.
    assert item["blob_url"].startswith("memory://tenant/")
    assert "ttl=" in item["blob_url"]


_INTERNAL_TENANT = "tnt_01J5TEST00000000000000TNT1"
_INTERNAL_USER = "usr_01J5TEST0000000000000USER1"


def _install_skill(
    client: TestClient,
    tenant_headers: dict[str, str],
    skill_id: str,
    *,
    version: str | None = None,
) -> None:
    body: dict[str, object] = {"enabled_by_default": True}
    if version is not None:
        body["version"] = version
    resp = client.post(
        f"/api/skills/v1/skills/{skill_id}/install",
        headers=tenant_headers,
        json=body,
    )
    assert resp.status_code == 201, resp.text


def test_internal_resolve_returns_description(
    app_client: TestClient,
    tenant_headers: dict[str, str],
    internal_headers: dict[str, str],
) -> None:
    # The resolved entry carries the manifest description (from SKILL.md
    # frontmatter — VALID_FRONTMATTER declares ``description: writes poetry``).
    skill = _create_skill(app_client, tenant_headers, visibility="private")
    _upload(app_client, tenant_headers, skill["id"], tarball=_valid_skill_tarball())
    _install_skill(app_client, tenant_headers, skill["id"], version="1.0.0")

    resp = app_client.get(
        "/api/skills/v1/internal/skills/resolve",
        params={
            "tenant_id": _INTERNAL_TENANT,
            "user_id": _INTERNAL_USER,
            "names": "poet",
        },
        headers=internal_headers,
    )
    assert resp.status_code == 200, resp.text
    item = resp.json()["items"][0]
    assert item["description"] == "writes poetry"


def _file_request(
    client: TestClient,
    internal_headers: dict[str, str],
    name: str,
    *,
    path: str | None = None,
) -> object:
    params: dict[str, str] = {
        "tenant_id": _INTERNAL_TENANT,
        "user_id": _INTERNAL_USER,
    }
    if path is not None:
        params["path"] = path
    return client.get(
        f"/api/skills/v1/internal/skills/{name}/file",
        params=params,
        headers=internal_headers,
    )


def test_internal_file_requires_scope(app_client: TestClient) -> None:
    resp = app_client.get(
        "/api/skills/v1/internal/skills/poet/file",
        params={"tenant_id": _INTERNAL_TENANT, "user_id": _INTERNAL_USER},
    )
    assert resp.status_code == 403


def test_internal_file_root_layout_skill_md(
    app_client: TestClient,
    tenant_headers: dict[str, str],
    internal_headers: dict[str, str],
) -> None:
    skill = _create_skill(app_client, tenant_headers, visibility="private")
    _upload(app_client, tenant_headers, skill["id"], tarball=_valid_skill_tarball())
    _install_skill(app_client, tenant_headers, skill["id"], version="1.0.0")

    resp = _file_request(app_client, internal_headers, "poet")
    assert resp.status_code == 200, resp.text  # type: ignore[attr-defined]
    body = resp.json()  # type: ignore[attr-defined]
    assert body["name"] == "poet"
    assert body["version"] == "1.0.0"
    assert body["sha256"]
    assert body["path"] == "SKILL.md"
    assert body["content"] == VALID_FRONTMATTER


def test_internal_file_subdir_layout_skill_md(
    app_client: TestClient,
    tenant_headers: dict[str, str],
    internal_headers: dict[str, str],
) -> None:
    # Single-subdir layout (<dir>/SKILL.md): path="SKILL.md" still resolves
    # relative to the skill root (the subdir).
    skill = _create_skill(app_client, tenant_headers, visibility="private")
    _upload(
        app_client, tenant_headers, skill["id"],
        tarball=_valid_skill_tarball(subdir="poet"),
    )
    _install_skill(app_client, tenant_headers, skill["id"], version="1.0.0")

    resp = _file_request(app_client, internal_headers, "poet", path="SKILL.md")
    assert resp.status_code == 200, resp.text  # type: ignore[attr-defined]
    body = resp.json()  # type: ignore[attr-defined]
    assert body["path"] == "SKILL.md"
    assert body["content"] == VALID_FRONTMATTER


def test_internal_file_reads_references(
    app_client: TestClient,
    tenant_headers: dict[str, str],
    internal_headers: dict[str, str],
) -> None:
    skill = _create_skill(app_client, tenant_headers, visibility="private")
    _upload(app_client, tenant_headers, skill["id"], tarball=_valid_skill_tarball())
    _install_skill(app_client, tenant_headers, skill["id"], version="1.0.0")

    resp = _file_request(
        app_client, internal_headers, "poet", path="references/notes.md",
    )
    assert resp.status_code == 200, resp.text  # type: ignore[attr-defined]
    body = resp.json()  # type: ignore[attr-defined]
    assert body["path"] == "references/notes.md"
    # _valid_skill_tarball writes ``see also`` into references/notes.md.
    assert body["content"] == "see also"


def test_internal_file_reads_references_subdir_layout(
    app_client: TestClient,
    tenant_headers: dict[str, str],
    internal_headers: dict[str, str],
) -> None:
    # references/ path resolves relative to the skill root even under a
    # single-subdir layout.
    skill = _create_skill(app_client, tenant_headers, visibility="private")
    _upload(
        app_client, tenant_headers, skill["id"],
        tarball=_valid_skill_tarball(subdir="poet"),
    )
    _install_skill(app_client, tenant_headers, skill["id"], version="1.0.0")

    resp = _file_request(
        app_client, internal_headers, "poet", path="references/notes.md",
    )
    assert resp.status_code == 200, resp.text  # type: ignore[attr-defined]
    assert resp.json()["content"] == "see also"  # type: ignore[attr-defined]


def test_internal_file_unknown_skill_404(
    app_client: TestClient,
    internal_headers: dict[str, str],
) -> None:
    # Skill name that the user has not installed → 404.
    resp = _file_request(app_client, internal_headers, "ghost")
    assert resp.status_code == 404, resp.text  # type: ignore[attr-defined]
    assert resp.json()["code"] == "SKILL.NOT_FOUND"  # type: ignore[attr-defined]


def test_internal_file_unknown_path_404(
    app_client: TestClient,
    tenant_headers: dict[str, str],
    internal_headers: dict[str, str],
) -> None:
    skill = _create_skill(app_client, tenant_headers, visibility="private")
    _upload(app_client, tenant_headers, skill["id"], tarball=_valid_skill_tarball())
    _install_skill(app_client, tenant_headers, skill["id"], version="1.0.0")

    resp = _file_request(
        app_client, internal_headers, "poet", path="references/missing.md",
    )
    assert resp.status_code == 404, resp.text  # type: ignore[attr-defined]
    assert resp.json()["code"] == "SKILL.FILE.NOT_FOUND"  # type: ignore[attr-defined]


def test_internal_file_rejects_path_traversal(
    app_client: TestClient,
    tenant_headers: dict[str, str],
    internal_headers: dict[str, str],
) -> None:
    skill = _create_skill(app_client, tenant_headers, visibility="private")
    _upload(app_client, tenant_headers, skill["id"], tarball=_valid_skill_tarball())
    _install_skill(app_client, tenant_headers, skill["id"], version="1.0.0")

    resp = _file_request(
        app_client, internal_headers, "poet", path="../secret.md",
    )
    assert resp.status_code == 400, resp.text  # type: ignore[attr-defined]
    assert resp.json()["code"] == "SKILL.FILE.PATH_INVALID"  # type: ignore[attr-defined]


def test_internal_file_non_utf8_returns_415(
    app_client: TestClient,
    tenant_headers: dict[str, str],
    internal_headers: dict[str, str],
) -> None:
    # A binary (non-UTF-8) member is rejected as unservable text (415).
    skill = _create_skill(app_client, tenant_headers, visibility="private")
    tarball = _make_tarball(
        {
            "SKILL.md": VALID_FRONTMATTER.encode("utf-8"),
            "assets/logo.bin": b"\xff\xfe\x00\x01binary",
        }
    )
    _upload(app_client, tenant_headers, skill["id"], tarball=tarball)
    _install_skill(app_client, tenant_headers, skill["id"], version="1.0.0")

    resp = _file_request(
        app_client, internal_headers, "poet", path="assets/logo.bin",
    )
    assert resp.status_code == 415, resp.text  # type: ignore[attr-defined]
    assert resp.json()["code"] == "SKILL.FILE.NOT_TEXT"  # type: ignore[attr-defined]


def test_internal_default_installed_requires_scope(app_client: TestClient) -> None:
    resp = app_client.get(
        "/api/skills/v1/internal/skills/default-installed",
        params={
            "tenant_id": "tnt_01J5TEST00000000000000TNT1",
            "user_id": "usr_01J5TEST0000000000000USER1",
        },
    )
    assert resp.status_code == 403


def test_internal_default_installed_filters_by_default(
    app_client: TestClient,
    tenant_headers: dict[str, str],
    internal_headers: dict[str, str],
) -> None:
    # Two installed skills: one default-enabled, one not.
    on = _create_skill(app_client, tenant_headers, name="poet", visibility="private")
    off = _create_skill(app_client, tenant_headers, name="ghost", visibility="private")
    assert app_client.post(
        f"/api/skills/v1/skills/{on['id']}/install",
        headers=tenant_headers,
        json={"enabled_by_default": True},
    ).status_code == 201
    assert app_client.post(
        f"/api/skills/v1/skills/{off['id']}/install",
        headers=tenant_headers,
        json={"enabled_by_default": False},
    ).status_code == 201

    resp = app_client.get(
        "/api/skills/v1/internal/skills/default-installed",
        params={
            "tenant_id": "tnt_01J5TEST00000000000000TNT1",
            "user_id": "usr_01J5TEST0000000000000USER1",
        },
        headers=internal_headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 1
    assert [item["name"] for item in body["items"]] == ["poet"]


def test_internal_endpoint_returns_skills_with_versions(
    app_client: TestClient,
    tenant_headers: dict[str, str],
    internal_headers: dict[str, str],
) -> None:
    skill = _create_skill(app_client, tenant_headers)
    _upload(app_client, tenant_headers, skill["id"], tarball=_valid_skill_tarball())
    resp = app_client.get(
        "/api/skills/v1/internal/skills",
        params={"tenant_id": "tnt_01J5TEST00000000000000TNT1", "names": "poet"},
        headers=internal_headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["versions"][0]["version"] == "1.0.0"
