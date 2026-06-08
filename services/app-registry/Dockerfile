# syntax=docker/dockerfile:1.7
# app-registry. Like skill-store, we pin FROM
# agent-platform-dev-session-orchestrator because Docker Desktop corrupts pypi
# metadata in this build network — the base image already carries fastapi,
# sqlalchemy[asyncio], asyncpg, alembic, httpx and foundation_sdk[fastapi].
# Unlike skill-store, app-registry has NO S3/blob deps, so there are no extra
# wheels to install.
FROM agent-platform-dev-session-orchestrator:latest

USER root
WORKDIR /app
RUN rm -rf /app/src /app/alembic /app/alembic.ini 2>/dev/null || true

COPY src/ ./src/
COPY alembic/ ./alembic/
COPY alembic.ini .
RUN chown -R app:app /app

ENV APP_REGISTRY_ENV=production \
    APP_REGISTRY_HTTP_PORT=8013 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

USER app
EXPOSE 8013

HEALTHCHECK --interval=10s --timeout=5s --retries=5 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8013/api/apps/v1/healthz').read()" || exit 1

CMD ["uvicorn", "app_registry.main:create_app", "--host", "0.0.0.0", "--port", "8013", "--factory"]
