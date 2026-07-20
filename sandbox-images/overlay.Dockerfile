# Source-only overlay rebuild: reuses the base image's already-installed
# Python deps and only swaps the in-repo agentscope_ext source files.
# Also installs boto3 (S3 attachments) and redis-py (hot-state checkpoints)
# so the runtime_worker can download user-uploaded attachments from S3/MinIO
# and write step/plan/cursor state to Redis for breakpoint resume.
#
# Example invocation (run from agent-platform repo root):
#   docker build \
#     -f sandbox-images/overlay.Dockerfile \
#     -t ghcr.io/agent-platform/agentscope-sandbox:0.1 \
#     --build-context agentscope_ext=./agentscope-ext \
#     sandbox-images/

ARG BASE_IMAGE=ghcr.io/agent-platform/agentscope-sandbox:0.1
FROM ${BASE_IMAGE}

USER root
COPY --from=agentscope_ext . /opt/agentscope-ext
RUN chown -R agent:agent /opt/agentscope-ext \
 && find /opt/agentscope-ext -name __pycache__ -exec rm -rf {} + 2>/dev/null || true \
 && pip install --quiet --no-cache-dir --disable-pip-version-check \
        boto3==1.35.0 \
        "redis>=5.0,<6.0" \
    || true
USER agent
WORKDIR /workspace
ENTRYPOINT ["python", "-m", "agentscope_ext.runtime_worker"]
