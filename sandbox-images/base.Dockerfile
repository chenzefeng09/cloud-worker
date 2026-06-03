# syntax=docker/dockerfile:1.7
# =============================================================================
# agentscope-sandbox:base — minimal AgentScope worker image (smoke-test tier)
#
# Build context expectations:
#   * primary context: ./sandbox-images (just for this Dockerfile + README)
#   * additional context "agentscope_ext": ../agentscope-ext (sources of the
#     in-repo agentscope_ext package, mounted via `--build-context`)
#
# Example invocation (run from agent-platform repo root):
#   docker build \
#     -f sandbox-images/base.Dockerfile \
#     -t ghcr.io/agent-platform/agentscope-sandbox:0.1 \
#     --build-context agentscope_ext=./agentscope-ext \
#     sandbox-images/
#
# Notes
# -----
# * Smoke-test scope: AgentScope + httpx + pydantic + pypdf.  Heavy extras
#   (Chromium/Playwright, LibreOffice, MATLAB) are intentionally absent —
#   they belong in office.Dockerfile / matlab.Dockerfile.  Skipping them
#   here keeps the image ~700 MB instead of ~1.6 GiB so the smoke-test
#   loop on a laptop stays in the single-digit-minute range.
# * Non-root ``agent`` user (uid 1000) per agent-platform convention;
#   /workspace is the bind-mount target the scheduler attaches to.
# =============================================================================

FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Build toolchain — needed by some Python wheels that fall back to source
# (notably anything with C extensions).  ca-certificates is required for
# pip + httpx TLS validation.  Lists are cleaned to keep the layer small.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Non-root user — uid 1000 matches the service containers so bind-mounted
# workspaces have consistent ownership on the host side.
RUN groupadd --system --gid 1000 agent \
 && useradd  --system --uid 1000 --gid agent --create-home --shell /usr/sbin/nologin agent \
 && mkdir -p /workspace \
 && chown -R agent:agent /workspace

# Pinned Python deps for the smoke tier.  AgentScope is the only one
# whose major version we pin tightly; the rest follow the same lower
# bounds the agentscope_ext package itself declares so a future bump in
# that pyproject.toml stays compatible.
RUN pip install --no-cache-dir \
        "agentscope>=2.0,<3.0" \
        "httpx>=0.27" \
        "pydantic>=2.7" \
        "pypdf"

# Pull in the in-repo agentscope_ext package via the additional build
# context so we don't bloat the primary context (which is just the
# sandbox-images/ folder).
COPY --from=agentscope_ext . /opt/agentscope-ext
RUN pip install --no-cache-dir -e /opt/agentscope-ext \
 && chown -R agent:agent /opt/agentscope-ext

USER agent
WORKDIR /workspace

# The scheduler writes /workspace/task.json before starting the container;
# runtime_worker.__main__ reads it and drives an AgentScope ReActAgent loop.
ENTRYPOINT ["python", "-m", "agentscope_ext.runtime_worker"]
