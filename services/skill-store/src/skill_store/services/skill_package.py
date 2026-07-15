"""Structural validation + safe parsing of uploaded skill packages.

A "skill" in this platform is a *pure instruction pack* following the Claude
skill convention: a gzip-compressed tar archive whose (sole) ``SKILL.md`` file
carries YAML frontmatter with at least a non-empty ``name`` and
``description``.  Optional ``scripts/``, ``references/`` and ``assets/``
directories may accompany it.

This module is the trust boundary for uploads.  The previous upload path
trusted the uploader's hand-filled manifest and never opened the tarball — a
malicious or malformed upload could land arbitrary bytes in the store and the
manifest could lie about the contents.  Here we:

1. Refuse anything that is not a valid gzip/tar archive.
2. Defend against decompression bombs and path-traversal *before* reading any
   member body (we stream member metadata via ``getmembers`` and accumulate
   size checks rather than ``extractall``).
3. Require exactly one ``SKILL.md`` and parse the *authoritative* metadata from
   its frontmatter, so the manifest we persist is server-derived, not
   uploader-supplied.

Every ``raise`` carries a Chinese ``user_message`` so the failure can be shown
verbatim to the uploader by the API/BFF layer.
"""

from __future__ import annotations

import hashlib
import io
import posixpath
import tarfile
from dataclasses import dataclass

import yaml

from skill_store.errors import (
    SkillFileNotFoundError,
    SkillFileNotTextError,
    SkillFilePathInvalidError,
    SkillFileTooLargeError,
    SkillPackageInvalidError,
)

# --- Anti-bomb / DoS thresholds -------------------------------------------
# These are the authoritative limits enforced at the storage boundary.  Block 2
# (BFF) and Block 4 (frontend) front-load a *byte* cap on the compressed upload;
# the limits here bound the *decompressed* footprint, which a small compressed
# tarball can blow past (the classic zip/gzip bomb).  Keep them as module-level
# constants so other blocks can import the exact values.
MAX_MEMBERS = 2000           # max number of entries in the archive
MAX_MEMBER_SIZE = 5 * 1024 * 1024      # 5 MiB — largest single decompressed member
MAX_TOTAL_SIZE = 50 * 1024 * 1024      # 50 MiB — largest total decompressed size
# Frontmatter description goes into every sandbox's system prompt verbatim as a
# one-line skill index entry.  Bound it so a single user can't blow up the
# context budget for everyone.  Align with the schema/column: the DB column
# skill_store.skills.description is varchar(2000) and SkillCreate.description's
# pydantic max_length is also 2000, so 2000 is the real ceiling — anything
# larger 500s on DB write.  (The old 200 was an isolated, over-tight limit.)
MAX_DESCRIPTION_LEN = 2000

# Name of the required metadata file (Claude skill convention).
SKILL_MD_FILENAME = "SKILL.md"


@dataclass(frozen=True)
class ParsedSkill:
    """Authoritative metadata extracted from a validated skill package.

    ``skill_md_path`` is the archive-relative path of the SKILL.md that was
    parsed (``SKILL.md`` for a root layout, ``<dir>/SKILL.md`` for a single
    subdirectory layout).  ``computed_sha256`` is the sha256 of the *raw
    uploaded bytes* (not the decompressed content) so it can be compared
    against an uploader-supplied ``expected_sha256`` and persisted as the
    canonical content hash.
    """

    name: str
    description: str
    skill_md_path: str
    computed_sha256: str


def validate_and_parse(tarball: bytes) -> ParsedSkill:
    """Validate ``tarball`` as a Claude skill package and parse its metadata.

    Raises :class:`SkillPackageInvalidError` (HTTP 400 at the API layer) on any
    structural or content problem.  Never lets a ``tarfile`` or ``yaml``
    exception escape as a 500 — those are caught and re-raised as
    ``SkillPackageInvalidError`` with an explanatory Chinese ``user_message``.
    """
    # 1) Must be a legal gzip-compressed tar.  We open from an in-memory buffer
    #    so we never touch the filesystem.  Any failure here means the bytes are
    #    not a gzip tar at all (corrupt, wrong format, truncated) — reject.
    try:
        tar = tarfile.open(fileobj=io.BytesIO(tarball), mode="r:gz")  # noqa: SIM115 — closed via `with tar` below
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise SkillPackageInvalidError(
            "无效的压缩包，必须是 gzip 压缩的 tar",
        ) from exc

    with tar:
        # 2) Walk member metadata once, enforcing anti-bomb / anti-traversal
        #    rules as we go.  Crucially we inspect ``getmembers()`` (header
        #    metadata only) and accumulate ``member.size`` rather than calling
        #    ``extractall`` — so a decompression bomb is rejected before its
        #    bytes are ever materialised.
        try:
            members = tar.getmembers()
        except (tarfile.TarError, OSError, EOFError) as exc:
            raise SkillPackageInvalidError(
                "无效的压缩包，必须是 gzip 压缩的 tar",
            ) from exc

        if len(members) > MAX_MEMBERS:
            raise SkillPackageInvalidError(
                f"压缩包成员数过多，最多允许 {MAX_MEMBERS} 个",
            )

        total_size = 0
        skill_md_paths: list[str] = []
        for member in members:
            name = member.name

            # Reject special members outright — symlinks/hardlinks can point
            # outside the extraction root, device/fifo nodes are never valid in
            # an instruction pack.
            if member.issym() or member.islnk():
                raise SkillPackageInvalidError(
                    f"压缩包不允许包含符号链接或硬链接：{name}",
                )
            if member.isdev():
                raise SkillPackageInvalidError(
                    f"压缩包不允许包含设备文件：{name}",
                )

            # Path-traversal / zip-slip defence: no absolute paths, no ".."
            # segment anywhere in the path.  We check normalised POSIX segments.
            if name.startswith("/"):
                raise SkillPackageInvalidError(
                    f"压缩包不允许包含绝对路径：{name}",
                )
            if ".." in name.replace("\\", "/").split("/"):
                raise SkillPackageInvalidError(
                    f"压缩包路径不允许包含 '..'：{name}",
                )

            # Per-member and cumulative decompressed-size caps.
            if member.size > MAX_MEMBER_SIZE:
                raise SkillPackageInvalidError(
                    f"压缩包单个文件过大，最大允许 {MAX_MEMBER_SIZE} 字节：{name}",
                )
            total_size += member.size
            if total_size > MAX_TOTAL_SIZE:
                raise SkillPackageInvalidError(
                    f"压缩包解压后总大小超过上限 {MAX_TOTAL_SIZE} 字节",
                )

            # 3) Track every regular file literally named SKILL.md, accepting a
            #    root layout (``SKILL.md``) or a single-subdirectory layout
            #    (``<dir>/SKILL.md``).  Deeper nesting is not a valid root.
            if member.isfile():
                parts = name.split("/")
                if parts[-1] == SKILL_MD_FILENAME and len(parts) <= 2:
                    skill_md_paths.append(name)

        # 3b) Exactly one SKILL.md must exist.
        if not skill_md_paths:
            raise SkillPackageInvalidError("缺少 SKILL.md")
        if len(skill_md_paths) > 1:
            raise SkillPackageInvalidError("包内存在多个 SKILL.md")
        skill_md_path = skill_md_paths[0]

        # 4) Read + parse the SKILL.md frontmatter.  The member is re-fetched by
        #    name and its body extracted via ``extractfile`` (bounded — we
        #    already capped its size above).
        skill_md_member = tar.getmember(skill_md_path)
        extracted = tar.extractfile(skill_md_member)
        if extracted is None:  # pragma: no cover — isfile() guaranteed above
            raise SkillPackageInvalidError("无法读取 SKILL.md 内容")
        raw = extracted.read()

    # Decode as UTF-8 — SKILL.md must be text.
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SkillPackageInvalidError(
            "SKILL.md 不是合法的 UTF-8 文本",
        ) from exc

    name, description = _parse_frontmatter(text)

    # 5) Canonical content hash of the *raw uploaded bytes*.
    computed_sha256 = hashlib.sha256(tarball).hexdigest()

    # 6) Return the authoritative parse result.
    return ParsedSkill(
        name=name,
        description=description,
        skill_md_path=skill_md_path,
        computed_sha256=computed_sha256,
    )


def _parse_frontmatter(text: str) -> tuple[str, str]:
    """Extract ``(name, description)`` from a SKILL.md YAML frontmatter block.

    The frontmatter is the region between the first two ``---`` fence lines at
    the top of the file.  ``yaml.safe_load`` parses only data (no arbitrary
    object construction), and any parse error is caught and converted to a
    ``SkillPackageInvalidError`` — a malformed document must never surface as a
    500.
    """
    # The document must *open* with a --- fence (allowing a leading BOM/newline).
    stripped = text.lstrip("﻿")
    if not stripped.startswith("---"):
        raise SkillPackageInvalidError(
            "SKILL.md 缺少 YAML frontmatter（必须以 --- 开头）",
        )

    # Find the closing fence: the first line that is exactly --- after the open.
    lines = stripped.splitlines()
    # lines[0] is the opening "---" (possibly with trailing spaces).
    closing_idx: int | None = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            closing_idx = i
            break
    if closing_idx is None:
        raise SkillPackageInvalidError(
            "SKILL.md frontmatter 未正确闭合（缺少结束的 ---）",
        )

    frontmatter_text = "\n".join(lines[1:closing_idx])

    try:
        data = yaml.safe_load(frontmatter_text)
    except yaml.YAMLError as exc:
        raise SkillPackageInvalidError(
            f"SKILL.md frontmatter 格式错误：{exc}",
        ) from exc

    if not isinstance(data, dict):
        raise SkillPackageInvalidError(
            "SKILL.md frontmatter 必须是键值对映射",
        )

    name = data.get("name")
    description = data.get("description")
    if not isinstance(name, str) or not name.strip():
        raise SkillPackageInvalidError(
            "SKILL.md frontmatter 缺少 name",
        )
    if not isinstance(description, str) or not description.strip():
        raise SkillPackageInvalidError(
            "SKILL.md frontmatter 缺少 description",
        )
    description_clean = description.strip()
    if len(description_clean) > MAX_DESCRIPTION_LEN:
        raise SkillPackageInvalidError(
            "SKILL.md frontmatter description 过长，"
            f"最多 {MAX_DESCRIPTION_LEN} 字符（当前 {len(description_clean)}）",
        )

    return name.strip(), description_clean


@dataclass(frozen=True)
class ExtractedFile:
    """A single member extracted from a skill package for in-process reading.

    ``path`` is the caller-facing relative path (relative to the skill root, i.e.
    the directory containing SKILL.md), normalised.  ``content`` is the decoded
    UTF-8 text of the member.
    """

    path: str
    content: str


def _skill_root_prefix(skill_md_path: str) -> str:
    """Archive-relative prefix of the skill root (dir containing SKILL.md).

    Root layout (``SKILL.md``) → ``""``; single-subdir layout
    (``<dir>/SKILL.md``) → ``"<dir>/"``.
    """
    parts = skill_md_path.split("/")
    if len(parts) <= 1:
        return ""
    return "/".join(parts[:-1]) + "/"


def _validate_request_path(path: str) -> str:
    """Reject absolute / traversal ``path`` and return a normalised relative path.

    Raises :class:`SkillFilePathInvalidError` (HTTP 400) on an absolute path or
    any ``..`` segment.  Returns the POSIX-normalised relative path (``\\`` folded
    to ``/``), which callers join against the skill root.
    """
    candidate = path.replace("\\", "/")
    if candidate.startswith("/"):
        raise SkillFilePathInvalidError(path)
    segments = candidate.split("/")
    if ".." in segments:
        raise SkillFilePathInvalidError(path)
    normalised = posixpath.normpath(candidate)
    # normpath can still surface a leading ``..`` (e.g. "a/../../b") — reject.
    if normalised.startswith("/") or normalised == ".." or normalised.startswith("../"):
        raise SkillFilePathInvalidError(path)
    if normalised == ".":
        raise SkillFilePathInvalidError(path)
    return normalised


def read_member(tarball: bytes, skill_md_path: str, path: str) -> ExtractedFile:
    """Extract a single member from a validated skill package as UTF-8 text.

    ``skill_md_path`` is the archive-relative path of the package's SKILL.md (as
    persisted in the server-authoritative manifest); ``path`` is resolved
    *relative to the skill root* (the directory containing SKILL.md), so
    ``path="SKILL.md"`` hits the package's SKILL.md under both a root layout and a
    single-subdirectory layout, and ``path="references/notes.md"`` resolves to
    ``<skill root>/references/notes.md``.

    Reuses the same tar safety posture as :func:`validate_and_parse`: opens in
    ``mode="r:gz"`` from an in-memory buffer, only regular-file members are
    served, and the per-member size cap is enforced.

    Raises:
        SkillFilePathInvalidError (400): ``path`` is absolute or contains ``..``.
        SkillFileNotFoundError (404): no regular-file member at the resolved path.
        SkillFileTooLargeError (413): the member exceeds ``MAX_MEMBER_SIZE``.
        SkillFileNotTextError (415): the member body is not valid UTF-8.
        SkillPackageInvalidError (400): the stored bytes are not a gzip tar.
    """
    normalised = _validate_request_path(path)
    prefix = _skill_root_prefix(skill_md_path)
    member_name = f"{prefix}{normalised}"

    try:
        tar = tarfile.open(fileobj=io.BytesIO(tarball), mode="r:gz")  # noqa: SIM115 — closed via `with tar`
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise SkillPackageInvalidError(
            "无效的压缩包，必须是 gzip 压缩的 tar",
        ) from exc

    with tar:
        try:
            member = tar.getmember(member_name)
        except KeyError as exc:
            raise SkillFileNotFoundError(normalised) from exc

        # Only regular files are servable — directories, symlinks, hardlinks and
        # device nodes are never a fetchable "file".
        if not member.isfile():
            raise SkillFileNotFoundError(normalised)
        if member.size > MAX_MEMBER_SIZE:
            raise SkillFileTooLargeError(normalised)

        extracted = tar.extractfile(member)
        if extracted is None:  # pragma: no cover — isfile() guaranteed above
            raise SkillFileNotFoundError(normalised)
        raw = extracted.read()

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SkillFileNotTextError(normalised) from exc

    return ExtractedFile(path=normalised, content=text)
