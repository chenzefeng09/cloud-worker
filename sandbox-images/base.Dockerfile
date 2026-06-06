# syntax=docker/dockerfile:1.7
# =============================================================================
# agentscope-sandbox:base — minimal AgentScope worker image (smoke-test tier)
#
# Build context expectations:
#   * primary context: ./sandbox-images (just for this Dockerfile + README)
#   * additional context "agentscope_ext": ../agentscope-ext (sources of the
#     in-repo agentscope_ext package, mounted via `--build-context`)
#   * additional context "office_wheels": ./sandbox-images/wheels (pre-fetched
#     Office library wheels, used for PyPI MITM workaround)
#
# Example invocation (run from agent-platform repo root):
#   docker build \
#     -f sandbox-images/base.Dockerfile \
#     -t ghcr.io/agent-platform/agentscope-sandbox:0.1 \
#     --build-context agentscope_ext=./agentscope-ext \
#     --build-context office_wheels=./sandbox-images/wheels \
#     sandbox-images/
#
# Notes
# -----
# * Smoke-test scope: AgentScope + httpx + pydantic + pypdf + python-docx +
#   openpyxl + python-pptx.  Heavy extras (Chromium/Playwright, LibreOffice,
#   MATLAB) are intentionally absent — they belong in office.Dockerfile /
#   matlab.Dockerfile.  Skipping them here keeps the image lean so the
#   smoke-test loop on a laptop stays in the single-digit-minute range.
# * Non-root ``agent`` user (uid 1000) per agent-platform convention;
#   /workspace is the bind-mount target the scheduler attaches to.
# * The agentscope + httpx + pydantic + pypdf layer is expensive to rebuild.
#   Under Docker Desktop's PyPI MITM environment, pip network calls fail with
#   JSON decode errors.  To avoid re-installing the heavy agentscope stack,
#   this Dockerfile layers on top of the already-built base image tag
#   (ARG BASE_IMAGE, default ghcr.io/agent-platform/agentscope-sandbox:0.1).
#   The Office libraries are installed from pre-fetched wheels in
#   sandbox-images/wheels/ (see --build-context office_wheels above).
#   To do a full from-scratch rebuild (e.g. upgrading agentscope), temporarily
#   change BASE_IMAGE to python:3.12-slim-bookworm and re-add the pip install
#   lines below before the Office block.
# =============================================================================

ARG BASE_IMAGE=ghcr.io/agent-platform/agentscope-sandbox:0.1

FROM ${BASE_IMAGE}

# ── Office document libraries ────────────────────────────────────────────────
# python-docx, openpyxl, python-pptx installed from pre-fetched wheels to work
# around Docker Desktop's PyPI MITM (same pattern as session-orchestrator).
# Wheels target linux/arm64 manylinux_2_28; refresh with:
#   pip download python-docx openpyxl python-pptx lxml et-xmlfile Pillow \
#     --dest sandbox-images/wheels \
#     --platform manylinux_2_28_aarch64 --python-version 312 \
#     --implementation cp --abi cp312 --only-binary=:all:
USER root
COPY --from=office_wheels . /tmp/wheels/
RUN pip install --no-cache-dir --no-deps /tmp/wheels/*.whl \
 && rm -rf /tmp/wheels

# Pull in updated agentscope_ext sources (editable install already in base).
COPY --from=agentscope_ext . /opt/agentscope-ext
RUN chown -R agent:agent /opt/agentscope-ext \
 && find /opt/agentscope-ext -name __pycache__ -exec rm -rf {} + 2>/dev/null || true

USER agent
WORKDIR /workspace

# The scheduler writes /workspace/task.json before starting the container;
# runtime_worker.__main__ reads it and drives an AgentScope ReActAgent loop.
ENTRYPOINT ["python", "-m", "agentscope_ext.runtime_worker"]
