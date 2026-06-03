# syntax=docker/dockerfile:1.7
# BFF is now built on top of agent-platform-dev-session-orchestrator because
# Docker Desktop on this host corrupts pypi metadata (bm2-bm3 fix-report) and
# the base already carries all the deps BFF needs (FastAPI + httpx +
# foundation_sdk + opentelemetry).  When the build network is fixed, restore
# the original `FROM python:3.12-slim` + poetry install variant.
FROM agent-platform-dev-session-orchestrator:latest

USER root
WORKDIR /app
RUN rm -rf /app/src /app/alembic /app/alembic.ini 2>/dev/null || true

# python-multipart is required by the attachment upload (multipart/form-data)
# proxy endpoint.  Installed from a pre-fetched wheel because Docker Desktop
# on this host MITM-corrupts PyPI metadata (bm2-bm3 fix-report).
COPY wheels/ /tmp/wheels/
RUN pip install --no-cache-dir --no-deps /tmp/wheels/*.whl \
 && rm -rf /tmp/wheels

COPY src/ ./src/
RUN chown -R app:app /app

ENV BFF_ENV=production \
    BFF_HTTP_PORT=8005 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

USER app
EXPOSE 8005

HEALTHCHECK --interval=10s --timeout=5s --retries=5 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8005/api/bff/v1/healthz').read()" || exit 1

CMD ["uvicorn", "agent_bff.main:create_app", "--host", "0.0.0.0", "--port", "8005", "--factory"]
