# syntax=docker/dockerfile:1.7
# Pin FROM agent-platform-dev-session-orchestrator: that image already has
# fastapi / sqlalchemy / asyncpg / alembic / foundation_sdk / opentelemetry
# pre-installed. Scheduler only needs the `docker` SDK + `websocket-client`
# on top; we install them from pre-fetched wheels in ./wheels because
# Docker Desktop's build network MITM rewrites pypi metadata + wheel bytes
# (bm2-bm3 fix-report).
FROM agent-platform-dev-session-orchestrator:latest

USER root
WORKDIR /app
RUN rm -rf /app/src 2>/dev/null || true

COPY wheels/ /tmp/wheels/
RUN pip install --no-cache-dir --no-deps /tmp/wheels/*.whl \
 && rm -rf /tmp/wheels

COPY src/ ./src/
RUN chown -R app:app /app

ENV SCHEDULER_ENV=production \
    SCHEDULER_HTTP_PORT=8007 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

USER app
EXPOSE 8007

HEALTHCHECK --interval=10s --timeout=5s --retries=5 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8007/api/scheduler/v1/healthz').read()" || exit 1

CMD ["uvicorn", "agent_scheduler.main:create_app", "--host", "0.0.0.0", "--port", "8007", "--factory"]
