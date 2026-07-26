#!/usr/bin/env python3
"""Seed the *platform-global default skill set* into a running skill-store.

WHAT "platform-global" MEANS
----------------------------
There is ONE shared set of default skills, owned by the reserved synthetic
tenant ``PLATFORM_TENANT_ID``.  Every organization (tenant) — including tenants
provisioned in the future — automatically sees these skills, and every user gets
them by default with **zero** per-user install rows.  Resolution is handled
server-side:

* skill-store's ``skills_tenant_select`` RLS policy is broadened so any tenant
  can *read* platform rows.
* ``resolve_for_runtime`` / ``read_skill_file`` fall back to a platform default
  when the user has no install of that name.
* ``GET /internal/skills/platform-defaults`` returns the platform default names
  to the orchestrator, which unions them into every run.

So — unlike the earlier per-tenant/per-user approach — this script does NOT
create per-tenant copies and does NOT install anything per user.  It creates the
13 skills exactly once, under the platform tenant, each flagged
``default_enabled=true``.

TWO MODES
---------
* ``--build`` (prep, run on an internet-connected dev host):
    Package the 13 skills from the source skills repo into
    ``services/skill-store/seed/skills/<name>.tgz`` (+ a ``manifest.json``).
    ``algorithmic-art`` is adapted for OFFLINE use first — p5.js is vendored into
    ``assets/p5.min.js`` and the SKILL.md / viewer.html are rewritten so nothing
    fetches cdnjs.  These tarballs are VENDORED into the repo so deployment is
    self-contained (no dependency on the external skills repo at deploy time).

* default (seed): read the vendored tarballs and seed them over HTTP against a
  running skill-store.  Idempotent (409-safe) — safe to run on every
  ``docker compose up``.

RE-RUN
------
    python3 services/skill-store/scripts/seed_default_skills.py            # seed
    python3 services/skill-store/scripts/seed_default_skills.py --build    # repackage vendored tgz
"""

from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tarfile
import tempfile
import time
from pathlib import Path

# --------------------------------------------------------------------------- #
# CONFIG — env-overridable                                                     #
# --------------------------------------------------------------------------- #

# The reserved platform tenant that owns the global default skills.  MUST stay
# byte-for-byte identical to skill_store.constants.PLATFORM_TENANT_ID.
PLATFORM_TENANT_ID = "tnt_00000000000000000PLATFORM"

BASE_URL = os.environ.get(
    "SKILL_STORE_BASE_URL", "http://localhost:8011/api/skills/v1"
).rstrip("/")

# Repo layout: this file is services/skill-store/scripts/seed_default_skills.py,
# so the vendored seed assets live at services/skill-store/seed/.
SEED_DIR = Path(
    os.environ.get("SEED_DIR", str(Path(__file__).resolve().parents[1] / "seed"))
)
SKILLS_TGZ_DIR = SEED_DIR / "skills"
MANIFEST_PATH = SEED_DIR / "manifest.json"

# Source repo + scratch build dir used ONLY in --build mode.
#
# ``SKILLS_SRC`` must point at a checkout of the upstream skills repo (the
# directory holding one sub-directory per skill).  There is no sensible default
# — set the env var when running ``--build``; run_build() fails loudly if the
# path does not exist.  Neither is needed for the normal (seed-from-tarball)
# mode, which reads the vendored assets under ``services/skill-store/seed/``.
SKILLS_SRC = Path(os.environ.get("SKILLS_SRC", "./skills"))
BUILD_DIR = Path(
    os.environ.get(
        "SEED_BUILD_DIR",
        str(Path(tempfile.gettempdir()) / "agent-platform-skill-seed"),
    )
)

VERSION = os.environ.get("SEED_SKILL_VERSION", "1.0.0")

# The exact 13 curated skills — no more, no less.  (Deliberately excluded:
# web-artifacts-builder, slack-gif-creator, webapp-testing, brand-guidelines.)
SKILL_NAMES = [
    "docx",
    "xlsx",
    "pptx",
    "pdf",
    "doc-coauthoring",
    "canvas-design",
    "theme-factory",
    "frontend-design",
    "internal-comms",
    "algorithmic-art",
    "claude-api",
    "mcp-builder",
    "skill-creator",
]

# X-User-Id sent on the create/upload calls.  X-Actor: ADMIN bypasses RBAC, so
# this is only recorded as ``uploaded_by``; any usr_-shaped id works.
SEED_USER = os.environ.get("SEED_USER", "usr_platform_seed")
OWNER = "platform-default"
VISIBILITY = "tenant"

# p5.js vendoring for the offline algorithmic-art adaptation (build mode only).
P5_VERSION = os.environ.get("P5_VERSION", "1.7.0")
P5_URL = os.environ.get(
    "P5_URL",
    f"https://cdnjs.cloudflare.com/ajax/libs/p5.js/{P5_VERSION}/p5.min.js",
)
CDN_SCRIPT_TAG = (
    f'<script src="https://cdnjs.cloudflare.com/ajax/libs/'
    f'p5.js/{P5_VERSION}/p5.min.js"></script>'
)

# --------------------------------------------------------------------------- #
# HTTP layer — prefer httpx, fall back to requests, then stdlib urllib.        #
# --------------------------------------------------------------------------- #

_BACKEND = None
try:
    import httpx  # type: ignore

    _BACKEND = "httpx"
except ImportError:  # pragma: no cover
    try:
        import requests  # type: ignore

        _BACKEND = "requests"
    except ImportError:  # pragma: no cover
        _BACKEND = "urllib"


class Resp:
    """Uniform response wrapper across the three backends."""

    def __init__(self, status: int, text: str):
        self.status = status
        self.text = text

    def json(self):
        return json.loads(self.text)


def _headers() -> dict[str, str]:
    """Create/upload headers — platform tenant + ADMIN actor (RBAC bypass)."""
    return {
        "X-Tenant-Id": PLATFORM_TENANT_ID,
        "X-User-Id": SEED_USER,
        "X-Actor": "ADMIN",
    }


def http_get(url: str, headers: dict[str, str]) -> Resp:
    if _BACKEND == "httpx":
        r = httpx.get(url, headers=headers, timeout=30.0)
        return Resp(r.status_code, r.text)
    if _BACKEND == "requests":
        r = requests.get(url, headers=headers, timeout=30.0)
        return Resp(r.status_code, r.text)
    import urllib.error
    import urllib.request

    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=30.0) as f:
            return Resp(f.status, f.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return Resp(e.code, e.read().decode("utf-8", "replace"))


def http_post_json(url: str, headers: dict[str, str], body: dict) -> Resp:
    if _BACKEND == "httpx":
        r = httpx.post(url, headers=headers, json=body, timeout=30.0)
        return Resp(r.status_code, r.text)
    if _BACKEND == "requests":
        r = requests.post(url, headers=headers, json=body, timeout=30.0)
        return Resp(r.status_code, r.text)
    import urllib.error
    import urllib.request

    data = json.dumps(body).encode("utf-8")
    h = {**headers, "Content-Type": "application/json"}
    req = urllib.request.Request(url, data=data, headers=h, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30.0) as f:
            return Resp(f.status, f.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return Resp(e.code, e.read().decode("utf-8", "replace"))


def http_post_multipart(
    url: str, headers: dict[str, str], fields: dict[str, str],
    file_field: str, filename: str, file_bytes: bytes,
) -> Resp:
    if _BACKEND == "httpx":
        r = httpx.post(
            url,
            headers=headers,
            data=fields,
            files={file_field: (filename, file_bytes, "application/gzip")},
            timeout=120.0,
        )
        return Resp(r.status_code, r.text)
    if _BACKEND == "requests":
        r = requests.post(
            url,
            headers=headers,
            data=fields,
            files={file_field: (filename, file_bytes, "application/gzip")},
            timeout=120.0,
        )
        return Resp(r.status_code, r.text)
    import urllib.error
    import urllib.request
    import uuid

    boundary = f"----seed{uuid.uuid4().hex}"
    parts: list[bytes] = []
    for k, v in fields.items():
        parts.append(f"--{boundary}\r\n".encode())
        parts.append(
            f'Content-Disposition: form-data; name="{k}"\r\n\r\n'.encode()
        )
        parts.append(f"{v}\r\n".encode())
    parts.append(f"--{boundary}\r\n".encode())
    parts.append(
        f'Content-Disposition: form-data; name="{file_field}"; '
        f'filename="{filename}"\r\n'.encode()
    )
    parts.append(b"Content-Type: application/gzip\r\n\r\n")
    parts.append(file_bytes)
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    data = b"".join(parts)
    h = {**headers, "Content-Type": f"multipart/form-data; boundary={boundary}"}
    req = urllib.request.Request(url, data=data, headers=h, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120.0) as f:
            return Resp(f.status, f.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return Resp(e.code, e.read().decode("utf-8", "replace"))


def download(url: str, dst: Path) -> int:
    """Download ``url`` into ``dst``.  Returns byte count.  Raises on failure."""
    if _BACKEND == "httpx":
        r = httpx.get(url, timeout=120.0, follow_redirects=True)
        if r.status_code != 200:
            raise RuntimeError(f"download {url} -> HTTP {r.status_code}")
        data = r.content
    elif _BACKEND == "requests":
        r = requests.get(url, timeout=120.0, allow_redirects=True)
        if r.status_code != 200:
            raise RuntimeError(f"download {url} -> HTTP {r.status_code}")
        data = r.content
    else:
        import urllib.request

        with urllib.request.urlopen(url, timeout=120.0) as f:
            data = f.read()
    dst.write_bytes(data)
    return len(data)


# --------------------------------------------------------------------------- #
# Build step (--build) — package the skills; adapt algorithmic-art for offline #
# --------------------------------------------------------------------------- #

def _replace_or_die(path: Path, old: str, new: str, label: str) -> None:
    text = path.read_text(encoding="utf-8")
    if old not in text:
        raise RuntimeError(
            f"algorithmic-art adaptation: expected substring not found "
            f"({label}) in {path}. Source wording may have drifted."
        )
    path.write_text(text.replace(old, new), encoding="utf-8")


def adapt_algorithmic_art(build_root: Path) -> Path:
    """Copy algorithmic-art into ``build_root`` and vendor p5.js for offline use.

    The generated art must not depend on a CDN.  We (1) copy the skill, (2)
    download p5.js into ``assets/p5.min.js``, (3) point ``templates/viewer.html``
    at the local file, (4) rewrite the SKILL.md guidance so generated artifacts
    inline/local-reference the vendored p5 instead of fetching cdnjs.  Source at
    ``SKILLS_SRC`` is never modified — everything happens in ``build_root``.
    """
    src = SKILLS_SRC / "algorithmic-art"
    dst = build_root / "algorithmic-art"
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)

    assets = dst / "assets"
    assets.mkdir(exist_ok=True)
    p5_dst = assets / "p5.min.js"
    n = download(P5_URL, p5_dst)
    head = p5_dst.read_bytes()[:64].decode("utf-8", "replace")
    if "p5.js" not in head or n < 100_000:
        raise RuntimeError(
            f"vendored p5 looks wrong (bytes={n}, head={head!r}); aborting "
            f"rather than shipping a broken offline skill."
        )
    print(f"    vendored p5.js {P5_VERSION} -> assets/p5.min.js ({n} bytes)")

    _replace_or_die(
        dst / "templates" / "viewer.html",
        CDN_SCRIPT_TAG,
        '<script src="../assets/p5.min.js"></script>',
        "viewer.html script tag",
    )

    skill_md = dst / "SKILL.md"
    _replace_or_die(
        skill_md,
        "The HTML artifact contains everything: p5.js (from CDN), the algorithm,"
        " parameter controls, and UI - all in one file that works immediately in"
        " claude.ai artifacts or any browser. Start from the template file, not"
        " from scratch.",
        "The HTML artifact contains everything: p5.js (vendored locally — inline"
        " the contents of `assets/p5.min.js`), the algorithm, parameter"
        " controls, and UI - all in one file that works immediately offline in"
        " any browser. Start from the template file, not from scratch.",
        "SKILL.md OUTPUT FORMAT prose",
    )
    _replace_or_die(
        skill_md,
        "  <!-- p5.js from CDN - always available -->\n"
        "  " + CDN_SCRIPT_TAG,
        "  <!-- p5.js vendored offline — inline the contents of "
        "assets/p5.min.js -->\n"
        "  <script>/* paste the full contents of assets/p5.min.js here "
        "(read the vendored file, do NOT fetch a CDN) */</script>",
        "SKILL.md artifact code example",
    )
    _replace_or_die(
        skill_md,
        "**CRITICAL**: This is a single artifact. No external files, no imports "
        "(except p5.js CDN). Everything inline.",
        "**CRITICAL**: This is a single artifact. No external files, no imports, "
        "no CDN — inline the vendored p5.js from `assets/p5.min.js`. "
        "Everything inline.",
        "SKILL.md CRITICAL note",
    )

    for f in dst.rglob("*"):
        if f.is_file():
            try:
                body = f.read_text(encoding="utf-8")
            except (UnicodeDecodeError, ValueError):
                continue
            if "cdnjs.cloudflare.com" in body:
                raise RuntimeError(
                    f"algorithmic-art still references cdnjs in {f}"
                )
    print("    rewrote viewer.html + SKILL.md; no cdnjs references remain")
    return dst


def _tar_filter(ti: tarfile.TarInfo):
    base = os.path.basename(ti.name)
    if base == ".DS_Store" or base.startswith("._"):
        return None
    parts = ti.name.split("/")
    if "__pycache__" in parts or base.endswith(".pyc"):
        return None
    ti.uid = ti.gid = 0
    ti.uname = ti.gname = ""
    return ti


def build_vendored_tarballs() -> None:
    """Package all 13 skills into ``SKILLS_TGZ_DIR/<name>.tgz`` + a manifest."""
    if not SKILLS_SRC.is_dir():
        raise RuntimeError(f"skills source not found: {SKILLS_SRC}")

    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    SKILLS_TGZ_DIR.mkdir(parents=True, exist_ok=True)
    print(f"[build] source     = {SKILLS_SRC}")
    print(f"[build] scratch    = {BUILD_DIR}")
    print(f"[build] vendor dir = {SKILLS_TGZ_DIR}")

    print("[build] adapting algorithmic-art for offline p5.js...")
    adapt_algorithmic_art(BUILD_DIR)

    manifest_skills: list[dict[str, str]] = []
    for name in SKILL_NAMES:
        root = BUILD_DIR if name == "algorithmic-art" else SKILLS_SRC
        skill_dir = root / name
        if not (skill_dir / "SKILL.md").is_file():
            raise RuntimeError(f"missing SKILL.md for {name} at {skill_dir}")
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            tar.add(skill_dir, arcname=name, filter=_tar_filter)
        data = buf.getvalue()
        (SKILLS_TGZ_DIR / f"{name}.tgz").write_bytes(data)
        manifest_skills.append({"name": name, "version": VERSION})
        print(f"[build] packaged {name:<18} {len(data):>9} bytes -> {name}.tgz")

    manifest = {
        "description": (
            "Platform-global default skill set. Seeded under the reserved "
            "platform tenant; every organization and user gets these by default."
        ),
        "platform_tenant_id": PLATFORM_TENANT_ID,
        "version": VERSION,
        "skills": manifest_skills,
    }
    MANIFEST_PATH.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"[build] wrote manifest -> {MANIFEST_PATH} ({len(manifest_skills)} skills)")


# --------------------------------------------------------------------------- #
# Seed step — create / upload against the live stack (platform tenant).        #
# --------------------------------------------------------------------------- #

def load_manifest_names() -> list[str]:
    """Names to seed — from the vendored manifest, falling back to SKILL_NAMES."""
    if MANIFEST_PATH.is_file():
        try:
            data = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
            names = [s["name"] for s in data.get("skills", []) if s.get("name")]
            if names:
                return names
        except Exception as exc:  # noqa: BLE001
            print(f"[seed] WARN: manifest unreadable ({exc}); using built-in list")
    return list(SKILL_NAMES)


def read_vendored_tarball(name: str) -> bytes:
    path = SKILLS_TGZ_DIR / f"{name}.tgz"
    if not path.is_file():
        raise RuntimeError(
            f"vendored tarball missing: {path}. Run with --build first."
        )
    return path.read_bytes()


def wait_for_ready(timeout_s: float = 120.0) -> None:
    """Block until skill-store's health endpoint answers (sidecar readiness)."""
    health = f"{BASE_URL}/healthz"
    deadline = time.time() + timeout_s
    attempt = 0
    while True:
        attempt += 1
        try:
            r = http_get(health, {})
            if r.status == 200:
                print(f"[seed] skill-store ready (after {attempt} probe(s))")
                return
        except Exception:  # noqa: BLE001
            pass
        if time.time() >= deadline:
            raise RuntimeError(
                f"skill-store not ready at {health} after {timeout_s:.0f}s"
            )
        time.sleep(2.0)


def ensure_skill(name: str) -> str:
    """Create the platform skill (default_enabled) or reuse on conflict."""
    resp = http_post_json(
        f"{BASE_URL}/skills",
        _headers(),
        {
            "name": name,
            "description": f"Platform default skill: {name}",
            "owner": OWNER,
            "visibility": VISIBILITY,
            "default_enabled": True,
        },
    )
    if resp.status in (200, 201):
        sid = resp.json()["id"]
        print(f"    create {name:<18} -> {sid}  [created]")
        return sid
    if resp.status == 409:
        listed = http_get(f"{BASE_URL}/skills", _headers())
        if listed.status != 200:
            raise RuntimeError(
                f"create {name} 409 but list failed HTTP {listed.status}: "
                f"{listed.text}"
            )
        for item in listed.json().get("items", []):
            if item.get("name") == name:
                sid = item["id"]
                print(f"    create {name:<18} -> {sid}  [exists]")
                return sid
        raise RuntimeError(
            f"create {name} 409 but not found in list; body={resp.text}"
        )
    raise RuntimeError(f"create {name} failed HTTP {resp.status}: {resp.text}")


def ensure_version(sid: str, name: str, tarball: bytes) -> str:
    """Upload version ``VERSION`` (skip on version conflict)."""
    resp = http_post_multipart(
        f"{BASE_URL}/skills/{sid}/versions",
        _headers(),
        {"version": VERSION},
        "file",
        f"{name}.tgz",
        tarball,
    )
    if resp.status in (200, 201):
        return "uploaded"
    if resp.status == 409:
        return "exists"
    raise RuntimeError(
        f"upload {name} v{VERSION} failed HTTP {resp.status}: {resp.text}"
    )


def seed() -> int:
    print(f"[seed] HTTP backend: {_BACKEND}")
    print(f"[seed] base URL:     {BASE_URL}")
    print(f"[seed] platform tenant: {PLATFORM_TENANT_ID}")

    wait_for_ready()

    names = load_manifest_names()
    print(f"[seed] seeding {len(names)} platform default skills\n")

    created = uploaded = existed_v = 0
    failures: list[str] = []
    for name in names:
        try:
            tarball = read_vendored_tarball(name)
            sid = ensure_skill(name)
            state = ensure_version(sid, name, tarball)
            if state == "uploaded":
                uploaded += 1
            else:
                existed_v += 1
                print(f"    version {name:<18} v{VERSION}  [exists, skip]")
            created += 1
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{name}: {exc}")
            print(f"    !! FAILED {name}: {exc}")

    print("\n" + "=" * 68)
    print("SUMMARY")
    print("=" * 68)
    print(
        f"platform skills present: {created}/{len(names)} "
        f"(versions uploaded={uploaded}, already-present={existed_v})"
    )
    if failures:
        print("\nFAILURES:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\nAll platform default skills created + uploaded successfully.")
    print("No per-user installs — resolution is platform-global and server-side.")
    return 0


def main(argv: list[str]) -> int:
    if "--build" in argv:
        build_vendored_tarballs()
        # If ONLY building was requested, stop here.  Otherwise fall through to
        # also seed (build-then-seed in one invocation).
        if "--seed" not in argv and "--build-and-seed" not in argv:
            return 0
    return seed()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
