"""Tests for ``agentscope_ext.skills_loader`` (progressive-disclosure model).

The loader no longer imports/executes any user-uploaded code.  It downloads a
skill tarball, verifies its sha256, unpacks it into a target dir, and reports
the absolute path to the contained ``SKILL.md`` so the worker can advertise it
to the agent via the system prompt.
"""

from __future__ import annotations

import hashlib
import importlib
import io
import sys
import tarfile
from pathlib import Path

import httpx
import pytest

_PKG_ROOT = Path(__file__).resolve().parent.parent
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))


@pytest.fixture
def fresh(tmp_path):
    import agentscope_ext.skills_loader as ext
    importlib.reload(ext)

    from agentscope_ext.skills_loader import loader
    importlib.reload(loader)
    # Re-bind the reloaded loader onto the package so ext.collect_skill_prompts
    # uses the same module instance our test patches.
    ext.loader = loader
    loader.set_skills_root(tmp_path / "skills")
    return ext, loader, tmp_path


def _make_skill_tarball(
    *, name: str, description: str, skill_md_path: str = "SKILL.md",
    extra: dict[str, str] | None = None,
) -> bytes:
    """Build a gzipped tarball with a SKILL.md (frontmatter) at ``skill_md_path``."""
    raw = io.BytesIO()
    md = (
        f"---\nname: {name}\ndescription: {description}\n---\n\n"
        f"# {name}\n\nDo the thing.\n"
    )
    with tarfile.open(fileobj=raw, mode="w:gz") as tar:
        data = md.encode("utf-8")
        info = tarfile.TarInfo(name=skill_md_path)
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
        for extra_name, extra_body in (extra or {}).items():
            b = extra_body.encode("utf-8")
            i = tarfile.TarInfo(name=extra_name)
            i.size = len(b)
            tar.addfile(i, io.BytesIO(b))
    return raw.getvalue()


def _make_traversal_tarball() -> bytes:
    """Tarball whose only member escapes the extraction dir (no SKILL.md)."""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w:gz") as tar:
        data = b"pwned"
        info = tarfile.TarInfo(name="../../evil.txt")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return raw.getvalue()


def _env(*, include_service_scopes: bool = True):
    env = {
        "MCP_REGISTRY_URL": "https://mcp-registry.local/api/mcp/v1",
        "SKILL_STORE_URL": "https://skill-store.local/api/skills/v1",
        "FOUNDATION_API_URL": "https://foundation.local",
        "FOUNDATION_API_TOKEN": "tok-xyz",
    }
    if include_service_scopes:
        env["FOUNDATION_SERVICE_SCOPES"] = "mcp.read_internal,skill.read_internal"
    return env


def _manifest(*, name, description, sha, version="1.0.0", skill_md_path="SKILL.md"):
    """A flattened install target as produced by select_version()."""
    return {
        "name": name,
        "description": description,
        "skill_md_path": skill_md_path,
        "version": version,
        "blob_url": f"https://cdn.local/{name}.tgz",
        "sha256": sha,
    }


# ---------------------------------------------------------------------------
# install_skill — unpack semantics
# ---------------------------------------------------------------------------


def test_install_unpacks_and_returns_skill_md_abspath(fresh):
    ext, loader, tmp_path = fresh
    tarball = _make_skill_tarball(name="poet", description="writes poetry")
    sha = hashlib.sha256(tarball).hexdigest()

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "cdn.local":
            return httpx.Response(200, content=tarball)
        return httpx.Response(404)

    loader.set_client(httpx.Client(transport=httpx.MockTransport(_handler)))
    result = loader.install_skill(
        _manifest(name="poet", description="writes poetry", sha=sha)
    )
    assert result is not None
    assert result["name"] == "poet"
    assert result["description"] == "writes poetry"
    abspath = Path(result["skill_md_abspath"])
    assert abspath.is_file()
    assert abspath == (tmp_path / "skills" / "poet-1.0.0" / "SKILL.md")
    assert "writes poetry" in abspath.read_text(encoding="utf-8")
    loader.set_client(None)


def test_install_subdir_skill_md_path(fresh):
    ext, loader, tmp_path = fresh
    tarball = _make_skill_tarball(
        name="nested", description="d", skill_md_path="nested/SKILL.md",
    )
    sha = hashlib.sha256(tarball).hexdigest()

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=tarball)

    loader.set_client(httpx.Client(transport=httpx.MockTransport(_handler)))
    result = loader.install_skill(
        _manifest(
            name="nested", description="d", sha=sha, skill_md_path="nested/SKILL.md",
        )
    )
    assert result is not None
    assert Path(result["skill_md_abspath"]).is_file()
    assert result["skill_md_abspath"].endswith("nested-1.0.0/nested/SKILL.md")
    loader.set_client(None)


def test_sha256_mismatch_returns_none(fresh, caplog):
    ext, loader, _ = fresh
    tarball = _make_skill_tarball(name="evil", description="d")

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=tarball)

    loader.set_client(httpx.Client(transport=httpx.MockTransport(_handler)))
    with caplog.at_level("WARNING"):
        result = loader.install_skill(
            _manifest(name="evil", description="d", sha="0" * 64)
        )
    assert result is None
    assert any("sha256 mismatch" in r.message for r in caplog.records)
    loader.set_client(None)


def test_missing_sha_returns_none(fresh, caplog):
    ext, loader, _ = fresh

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x")

    loader.set_client(httpx.Client(transport=httpx.MockTransport(_handler)))
    target = _manifest(name="nosha", description="d", sha="")
    with caplog.at_level("WARNING"):
        result = loader.install_skill(target)
    assert result is None
    assert any("no sha256" in r.message for r in caplog.records)
    loader.set_client(None)


def test_missing_skill_md_returns_none(fresh, caplog):
    ext, loader, _ = fresh
    # Valid tarball but SKILL.md lives elsewhere than the manifest claims.
    tarball = _make_skill_tarball(
        name="x", description="d", skill_md_path="OTHER.md",
    )
    sha = hashlib.sha256(tarball).hexdigest()

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=tarball)

    loader.set_client(httpx.Client(transport=httpx.MockTransport(_handler)))
    with caplog.at_level("WARNING"):
        # Manifest says SKILL.md but tarball only has OTHER.md.
        result = loader.install_skill(_manifest(name="x", description="d", sha=sha))
    assert result is None
    assert any("SKILL.md not found" in r.message for r in caplog.records)
    loader.set_client(None)


def test_path_traversal_tarball_does_not_crash(fresh):
    ext, loader, _ = fresh
    tarball = _make_traversal_tarball()
    sha = hashlib.sha256(tarball).hexdigest()

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=tarball)

    loader.set_client(httpx.Client(transport=httpx.MockTransport(_handler)))
    # data filter drops/neutralises the escaping member; SKILL.md absent → None.
    result = loader.install_skill(_manifest(name="trav", description="d", sha=sha))
    assert result is None
    loader.set_client(None)


def test_download_failure_returns_none(fresh, caplog):
    ext, loader, _ = fresh

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    loader.set_client(httpx.Client(transport=httpx.MockTransport(_handler)))
    with caplog.at_level("WARNING"):
        result = loader.install_skill(_manifest(name="dl", description="d", sha="a" * 64))
    assert result is None
    assert any("download dl failed" in r.message for r in caplog.records)
    loader.set_client(None)


# ---------------------------------------------------------------------------
# select_version — flatten internal-endpoint entry
# ---------------------------------------------------------------------------


def test_select_version_flattens_internal_entry(fresh):
    _, loader, _ = fresh
    entry = {
        "name": "poet",
        "description": "skill-level desc",
        "versions": [
            {
                "version": "1.0.0",
                "manifest": {
                    "name": "poet",
                    "description": "manifest desc",
                    "skill_md_path": "SKILL.md",
                    "format": "skill_md_v1",
                    "sha256": "abc",
                },
                "blob_url": "https://cdn.local/poet.tgz",
                "sha256": "abc",
            }
        ],
    }
    target = loader.select_version(entry)
    assert target == {
        "name": "poet",
        "description": "manifest desc",
        "skill_md_path": "SKILL.md",
        "version": "1.0.0",
        "blob_url": "https://cdn.local/poet.tgz",
        "sha256": "abc",
    }


def test_select_version_no_versions_returns_none(fresh):
    _, loader, _ = fresh
    assert loader.select_version({"name": "x", "versions": []}) is None


# ---------------------------------------------------------------------------
# collect_skill_prompts — end-to-end, failure-open
# ---------------------------------------------------------------------------


def test_collect_skill_prompts_returns_two(fresh):
    ext, loader, _ = fresh
    tb_a = _make_skill_tarball(name="poet", description="writes poetry")
    tb_b = _make_skill_tarball(name="chef", description="cooks recipes")
    sha_a = hashlib.sha256(tb_a).hexdigest()
    sha_b = hashlib.sha256(tb_b).hexdigest()

    def _entry(name, desc, sha, host):
        return {
            "name": name,
            "description": desc,
            "versions": [
                {
                    "version": "1.0.0",
                    "manifest": {
                        "name": name, "description": desc,
                        "skill_md_path": "SKILL.md",
                        "format": "skill_md_v1", "sha256": sha,
                    },
                    "blob_url": f"https://{host}/pkg.tgz",
                    "sha256": sha,
                }
            ],
        }

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/skills/v1/internal/skills":
            assert request.url.params["tenant_id"] == "tnt-abc"
            return httpx.Response(200, json={"skills": [
                _entry("poet", "writes poetry", sha_a, "a.local"),
                _entry("chef", "cooks recipes", sha_b, "b.local"),
            ]})
        if request.url.host == "a.local":
            return httpx.Response(200, content=tb_a)
        if request.url.host == "b.local":
            return httpx.Response(200, content=tb_b)
        return httpx.Response(404)

    loader.set_client(httpx.Client(transport=httpx.MockTransport(_handler)))
    prompts = ext.collect_skill_prompts(
        {"skills": ["poet", "chef"]}, "tnt-abc", _env(),
    )
    assert len(prompts) == 2
    names = {p["name"] for p in prompts}
    assert names == {"poet", "chef"}
    for p in prompts:
        assert Path(p["skill_md_abspath"]).is_file()
    loader.set_client(None)


def test_collect_skill_prompts_failure_open(fresh, caplog):
    """One skill downloads fine, the other 500s on download → only the good one."""
    ext, loader, _ = fresh
    tb_good = _make_skill_tarball(name="good", description="ok")
    sha_good = hashlib.sha256(tb_good).hexdigest()

    def _entry(name, desc, sha, host):
        return {
            "name": name, "description": desc,
            "versions": [{
                "version": "1.0.0",
                "manifest": {
                    "name": name, "description": desc,
                    "skill_md_path": "SKILL.md",
                    "format": "skill_md_v1", "sha256": sha,
                },
                "blob_url": f"https://{host}/pkg.tgz",
                "sha256": sha,
            }],
        }

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/skills/v1/internal/skills":
            return httpx.Response(200, json={"skills": [
                _entry("good", "ok", sha_good, "good.local"),
                _entry("bad", "broken", "f" * 64, "bad.local"),
            ]})
        if request.url.host == "good.local":
            return httpx.Response(200, content=tb_good)
        return httpx.Response(500)  # bad.local download fails

    loader.set_client(httpx.Client(transport=httpx.MockTransport(_handler)))
    with caplog.at_level("WARNING"):
        prompts = ext.collect_skill_prompts(
            {"skills": ["good", "bad"]}, "tnt-abc", _env(),
        )
    assert [p["name"] for p in prompts] == ["good"]
    loader.set_client(None)


def test_collect_no_skills_is_empty(fresh):
    ext, loader, _ = fresh
    assert ext.collect_skill_prompts({}, "tnt-abc", _env()) == []


def test_collect_manifest_fetch_failure_is_empty(fresh, caplog):
    """A 403 (no service scopes) on the manifest fetch → [] not a crash."""
    ext, loader, _ = fresh

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": "no scope"})

    loader.set_client(httpx.Client(transport=httpx.MockTransport(_handler)))
    with caplog.at_level("WARNING"):
        prompts = ext.collect_skill_prompts(
            {"skills": ["x"]}, "tnt-abc", _env(include_service_scopes=False),
        )
    assert prompts == []
    assert any("manifest fetch failed" in r.message for r in caplog.records)
    loader.set_client(None)


def test_install_is_noop(fresh):
    """install() must stay callable (bootstrap contract) and not touch hooks."""
    ext, _, _ = fresh
    assert ext.install() is None


def test_fetch_sends_x_service_scopes_header(fresh):
    ext, loader, _ = fresh
    captured: dict[str, str] = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/skills/v1/internal/skills":
            captured.update(dict(request.headers))
            return httpx.Response(200, json={"skills": []})
        return httpx.Response(404)

    loader.set_client(httpx.Client(transport=httpx.MockTransport(_handler)))
    ext.collect_skill_prompts({"skills": ["x"]}, "tnt-abc", _env())
    assert "x-service-scopes" in captured
    assert "skill.read_internal" in captured["x-service-scopes"]
    assert captured.get("x-tenant-id") == "tnt-abc"
    loader.set_client(None)
