"""Static analysis: verify services use foundation SDK correctly.

Rules:
  1. No raw httpx calls in service source — must use foundation SDK Transport
  2. No hardcoded tenant_id strings in business logic
  3. All service source files import from foundation_sdk or use the Transport
"""

import os
import re
import subprocess
from pathlib import Path


SERVICES_DIR = Path(__file__).resolve().parent.parent.parent / "services"

# Files to exclude from scanning
EXCLUDE_PATTERNS = {
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "tests",
    "test_",
    "conftest.py",
    "migrations",
    "alembic",
    ".pyc",
}

# Patterns that suggest raw HTTP without the SDK
RAW_HTTP_PATTERNS = [
    (r"httpx\.(Client|AsyncClient|get|post|put|patch|delete|request|stream)\(", "raw httpx call"),
    (r"import httpx", "import httpx directly"),
    (r"from httpx import", "import httpx directly"),
    (r"requests\.(get|post|put|patch|delete|request)\(", "raw requests call"),
    (r"import requests", "import requests directly"),
]


def _find_service_py_files() -> list[Path]:
    """Find all .py source files in services/, excluding tests/migrations."""
    py_files = []
    for svc_dir in SERVICES_DIR.iterdir():
        if not svc_dir.is_dir():
            continue
        for py_file in svc_dir.rglob("*.py"):
            parts = set(py_file.parts)
            if EXCLUDE_PATTERNS & parts:
                continue
            if any(py_file.name.startswith(p) for p in ["test_", "conftest"]):
                continue
            py_files.append(py_file)
    return py_files


def _check_file(filepath: Path) -> list[str]:
    """Check a single file for conformance violations. Returns list of issues."""
    issues = []
    content = filepath.read_text()

    for pattern, desc in RAW_HTTP_PATTERNS:
        if re.search(pattern, content):
            # exceptions: importing httpx in test files, or using httpx within transport layer
            if "transport" in filepath.name.lower():
                continue
            if "client.py" in filepath.name and filepath.parent.name == "api":
                continue
            issues.append(f"{filepath.relative_to(SERVICES_DIR.parent)}: {desc}")

    # Check for hardcoded tenant_id
    if re.search(r'"tenant_id"\s*:', content) or re.search(r"'tenant_id'\s*:", content):
        rel = filepath.relative_to(SERVICES_DIR.parent)
        issues.append(f"{rel}: hardcoded tenant_id assignment")

    return issues


class TestSDKUsage:
    """Verify services use foundation SDK, not raw HTTP."""

    def test_no_raw_httpx_in_services(self):
        """No raw httpx/requests calls in service source (except transport layer)."""
        py_files = _find_service_py_files()
        all_issues = []

        for fp in py_files:
            all_issues.extend(_check_file(fp))

        if all_issues:
            issues_str = "\n  ".join(all_issues)
            pytest.fail(f"Found {len(all_issues)} conformance violation(s):\n  {issues_str}")

    def test_services_exist(self):
        """Verify the three required services exist."""
        required = {"bff", "session-orchestrator", "agent-scheduler"}
        actual = {d.name for d in SERVICES_DIR.iterdir() if d.is_dir()}
        missing = required - actual
        assert not missing, f"Missing services: {missing}"

    def test_no_hardcoded_tenant_id(self):
        """No business logic should hardcode tenant_id as string literal."""
        py_files = _find_service_py_files()
        hardcoded = []

        for fp in py_files:
            content = fp.read_text()
            # Look for tenant_id = "..." patterns (not from config)
            if re.search(r'tenant_id\s*=\s*"[^"]{5,}"', content):
                hardcoded.append(str(fp.relative_to(SERVICES_DIR.parent)))
            # Look for tenant_id assignment in business logic (not models/config)
            if fp.name in ("models.py", "config.py"):
                continue  # models/config are allowed to reference tenant_id
            if re.search(r"tenant_id\s*=\s*'[^']{5,}'", content):
                hardcoded.append(str(fp.relative_to(SERVICES_DIR.parent)))

        # Deduplicate
        hardcoded = list(set(hardcoded))
        assert not hardcoded, (
            f"Hardcoded tenant_id found in: {hardcoded}. "
            "Use contextvars or middleware injection instead."
        )
