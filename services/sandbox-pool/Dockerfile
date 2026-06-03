# syntax=docker/dockerfile:1.7
# Overlay pattern: base image already has fastapi/sqlalchemy/asyncpg/alembic/
# foundation_sdk/opentelemetry/httpx. sandbox-pool needs docker SDK +
# websocket-client + prometheus_client on top; install from pre-fetched wheels
# to avoid the Docker Desktop PyPI MITM issue (see services/agent-scheduler/Dockerfile).
FROM agent-platform-dev-session-orchestrator:latest

USER root
WORKDIR /app
RUN rm -rf /app/src 2>/dev/null || true

COPY wheels/ /tmp/wheels/
RUN pip install --no-cache-dir --no-deps /tmp/wheels/*.whl \
 && rm -rf /tmp/wheels

COPY src/ ./src/
RUN chown -R app:app /app

ENV SANDBOX_POOL_ENV=production \
    SANDBOX_POOL_HTTP_PORT=8008 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

USER app
EXPOSE 8008

HEALTHCHECK --interval=10s --timeout=5s --retries=5 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8008/api/sandbox-pool/v1/healthz').read()" || exit 1

CMD ["uvicorn", "sandbox_pool.main:create_app", "--host", "0.0.0.0", "--port", "8008", "--factory"]
