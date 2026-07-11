# syntax=docker/dockerfile:1.7
# =============================================================================
# From-scratch bootstrap for ghcr.io/agent-platform/agentscope-sandbox:0.1
#
# The repo's base.Dockerfile is a self-layering patch (FROM its own tag) and
# the original bootstrap layer was never committed; when the local image gets
# pruned there is nothing to layer on.  This file reconstructs the base per
# the notes in base.Dockerfile:
#   python:3.12-slim-bookworm + agentscope stack + office wheels
#   + non-root ``agent`` user (uid 1000) + /workspace + editable agentscope-ext
# boto3/redis are included here because overlay.Dockerfile installs them with
# ``|| true`` — if they are missing from the base a silent overlay failure
# crashes the worker at import time.
#
# Build (from agent-platform repo root):
#   docker build -f <this file> \
#     -t ghcr.io/agent-platform/agentscope-sandbox:0.1 \
#     --build-context agentscope_ext=./agentscope-ext \
#     --build-context office_wheels=./sandbox-images/wheels \
#     sandbox-images/
# =============================================================================

FROM python:3.12-slim-bookworm

RUN groupadd -g 1000 agent \
 && useradd -m -u 1000 -g 1000 agent \
 && mkdir -p /workspace \
 && chown agent:agent /workspace

# agentscope pinned to 2.0.4 — the version whose _agent.py context-ownership
# semantics (_get_last_msg/_save_to_context name matching) the worker's
# history-restore path was verified against.
RUN pip install --no-cache-dir --disable-pip-version-check \
      "agentscope==2.0.4" \
      "httpx>=0.27" \
      "pydantic>=2.7" \
      "pyyaml>=6.0.2,<7" \
      "pypdf" \
      "mcp" \
      "boto3==1.35.0" \
      "redis>=5.0,<6.0"

# Office document libraries from the pre-fetched wheels dir (parity with the
# old base; wheels are linux/arm64 cp312).
COPY --from=office_wheels . /tmp/wheels/
RUN pip install --no-cache-dir --no-deps /tmp/wheels/*.whl \
 && rm -rf /tmp/wheels

# Editable install of the in-repo overlay package (deps already satisfied
# above; overlay.Dockerfile refreshes the sources on every source-only build).
COPY --from=agentscope_ext . /opt/agentscope-ext
RUN pip install --no-cache-dir --no-deps -e /opt/agentscope-ext \
 && chown -R agent:agent /opt/agentscope-ext \
 && find /opt/agentscope-ext -name __pycache__ -exec rm -rf {} + 2>/dev/null || true

USER agent
WORKDIR /workspace

ENTRYPOINT ["python", "-m", "agentscope_ext.runtime_worker"]
