"""Tests for ``agentscope_ext.skills_loader`` — mount-scanning model.

The loader no longer downloads skill tarballs.  Skills are bind-mounted
read-only at ``<SANDBOX_SKILLS_MOUNT_ROOT>/<name>/`` by the scheduler, and
this module just walks the mount looking for ``SKILL.md`` and parses its
YAML frontmatter.  Failure-open throughout: missing mounts, malformed
frontmatter, exceptions — log a warning and skip; never crash the worker.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_PKG_ROOT = Path(__file__).resolve().parent.parent
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

from agentscope_ext import skills_loader  # noqa: E402


def _make_mount_dir(
    mount_root: Path,
    name: str,
    *,
    frontmatter: str | None,
    body: str = "",
    subdir: str | None = None,
) -> Path:
    """Create ``<mount_root>/<name>[/<subdir>]/SKILL.md`` with given content.

    ``frontmatter`` is the raw YAML body between the ``---`` fences (or
    ``None`` to produce a SKILL.md with no fences at all).  Returns the
    path of the SKILL.md that was written.
    """
    target_dir = mount_root / name
    if subdir:
        target_dir = target_dir / subdir
    target_dir.mkdir(parents=True, exist_ok=True)
    skill_md = target_dir / "SKILL.md"
    if frontmatter is None:
        skill_md.write_text(body, encoding="utf-8")
    else:
        skill_md.write_text(f"---\n{frontmatter}\n---\n\n{body}", encoding="utf-8")
    return skill_md


def _env(mount_root: Path) -> dict[str, str]:
    return {"SANDBOX_SKILLS_MOUNT_ROOT": str(mount_root)}


# ---------------------------------------------------------------------------
# collect_skill_prompts — happy paths
# ---------------------------------------------------------------------------


def test_root_layout_returns_one_entry(tmp_path):
    mount_root = tmp_path / "mounts"
    skill_md = _make_mount_dir(
        mount_root, "poet", frontmatter="name: poet\ndescription: writes poetry",
    )

    prompts = skills_loader.collect_skill_prompts(
        {"skills": ["poet"]}, "tnt-abc", _env(mount_root),
    )

    assert prompts == [
        {
            "name": "poet",
            "description": "writes poetry",
            "skill_md_abspath": str(skill_md.resolve()),
        }
    ]


def test_subdir_layout_is_found(tmp_path):
    mount_root = tmp_path / "mounts"
    skill_md = _make_mount_dir(
        mount_root,
        "nested",
        frontmatter="name: nested\ndescription: a nested skill",
        subdir="inner",
    )

    prompts = skills_loader.collect_skill_prompts(
        {"skills": ["nested"]}, "tnt-abc", _env(mount_root),
    )

    assert len(prompts) == 1
    assert prompts[0]["name"] == "nested"
    assert prompts[0]["description"] == "a nested skill"
    assert prompts[0]["skill_md_abspath"] == str(skill_md.resolve())
    assert prompts[0]["skill_md_abspath"].endswith("/nested/inner/SKILL.md")


def test_multiple_skills_each_resolved_independently(tmp_path):
    mount_root = tmp_path / "mounts"
    _make_mount_dir(
        mount_root, "poet", frontmatter="name: poet\ndescription: writes poetry",
    )
    _make_mount_dir(
        mount_root, "chef",
        frontmatter="name: chef\ndescription: cooks recipes",
        subdir="pkg",
    )

    prompts = skills_loader.collect_skill_prompts(
        {"skills": ["poet", "chef"]}, "tnt-abc", _env(mount_root),
    )
    by_name = {p["name"]: p for p in prompts}
    assert set(by_name) == {"poet", "chef"}
    assert by_name["poet"]["description"] == "writes poetry"
    assert by_name["chef"]["description"] == "cooks recipes"


# ---------------------------------------------------------------------------
# collect_skill_prompts — failure-open paths
# ---------------------------------------------------------------------------


def test_missing_mount_dir_skipped_with_warning(tmp_path, caplog):
    mount_root = tmp_path / "mounts"
    mount_root.mkdir()
    # Mount 'poet' but ask for 'poet' + 'missing'.
    _make_mount_dir(
        mount_root, "poet", frontmatter="name: poet\ndescription: writes poetry",
    )

    with caplog.at_level("WARNING"):
        prompts = skills_loader.collect_skill_prompts(
            {"skills": ["poet", "missing"]}, "tnt-abc", _env(mount_root),
        )

    assert [p["name"] for p in prompts] == ["poet"]
    assert any("not mounted" in r.message for r in caplog.records)


def test_missing_skill_md_skipped_with_warning(tmp_path, caplog):
    mount_root = tmp_path / "mounts"
    # Mount dir exists but is empty.
    (mount_root / "empty").mkdir(parents=True)

    with caplog.at_level("WARNING"):
        prompts = skills_loader.collect_skill_prompts(
            {"skills": ["empty"]}, "tnt-abc", _env(mount_root),
        )

    assert prompts == []
    assert any("SKILL.md not found" in r.message for r in caplog.records)


def test_missing_description_still_returns_entry_with_empty_desc(tmp_path, caplog):
    mount_root = tmp_path / "mounts"
    skill_md = _make_mount_dir(
        mount_root, "noddy", frontmatter="name: noddy",
    )

    with caplog.at_level("WARNING"):
        prompts = skills_loader.collect_skill_prompts(
            {"skills": ["noddy"]}, "tnt-abc", _env(mount_root),
        )

    assert prompts == [
        {
            "name": "noddy",
            "description": "",
            "skill_md_abspath": str(skill_md.resolve()),
        }
    ]
    assert any("empty/missing description" in r.message for r in caplog.records)


def test_malformed_frontmatter_skipped_with_warning(tmp_path, caplog):
    mount_root = tmp_path / "mounts"
    # YAML syntax error: unmatched bracket.
    _make_mount_dir(
        mount_root, "bad", frontmatter="name: [unclosed\ndescription: x",
    )

    with caplog.at_level("WARNING"):
        prompts = skills_loader.collect_skill_prompts(
            {"skills": ["bad"]}, "tnt-abc", _env(mount_root),
        )

    assert prompts == []
    assert any("malformed/missing frontmatter" in r.message for r in caplog.records)


def test_no_frontmatter_at_all_skipped(tmp_path, caplog):
    mount_root = tmp_path / "mounts"
    _make_mount_dir(
        mount_root, "plain", frontmatter=None, body="# Just a heading\n",
    )

    with caplog.at_level("WARNING"):
        prompts = skills_loader.collect_skill_prompts(
            {"skills": ["plain"]}, "tnt-abc", _env(mount_root),
        )

    assert prompts == []
    assert any("malformed/missing frontmatter" in r.message for r in caplog.records)


def test_empty_skills_returns_empty(tmp_path):
    mount_root = tmp_path / "mounts"
    mount_root.mkdir()
    assert (
        skills_loader.collect_skill_prompts({}, "tnt-abc", _env(mount_root)) == []
    )
    assert (
        skills_loader.collect_skill_prompts(
            {"skills": []}, "tnt-abc", _env(mount_root),
        )
        == []
    )


def test_missing_mount_root_returns_empty(tmp_path, caplog):
    mount_root = tmp_path / "does-not-exist"

    with caplog.at_level("WARNING"):
        prompts = skills_loader.collect_skill_prompts(
            {"skills": ["poet"]}, "tnt-abc", _env(mount_root),
        )

    assert prompts == []
    assert any("mount root" in r.message for r in caplog.records)


def test_default_mount_root_used_when_env_missing(tmp_path, monkeypatch):
    """If SANDBOX_SKILLS_MOUNT_ROOT is unset, the default '/home/skills' is used.

    We don't have /home/skills on the test host, so the call should just
    return [] (failure-open) — proving the default kicked in without crash.
    """
    prompts = skills_loader.collect_skill_prompts(
        {"skills": ["whatever"]}, "tnt-abc", {},
    )
    assert prompts == []


# ---------------------------------------------------------------------------
# install() contract — still a no-op
# ---------------------------------------------------------------------------


def test_install_is_noop():
    assert skills_loader.install() is None
