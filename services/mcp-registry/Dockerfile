# syntax=docker/dockerfile:1.7
#
# B-M3 mcp-registry. We pin FROM agent-platform-dev-session-orchestrator
# because Docker Desktop on this host corrupts pypi metadata in the build
# network (bm2-bm3 fix-report) and the session-orchestrator image already
# carries the full FastAPI / SQLAlchemy / asyncpg / alembic / foundation_sdk
# / opentelemetry stack we need.
FROM agent-platform-dev-session-orchestrator:latest

USER root
WORKDIR /app
RUN rm -rf /app/src /app/alembic /app/alembic.ini 2>/dev/null || true

COPY src/ ./src/
COPY alembic/ ./alembic/
COPY alembic.ini .
RUN chown -R app:app /app

ENV MCP_REGISTRY_ENV=production \
    MCP_REGISTRY_HTTP_PORT=8010 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

USER app
EXPOSE 8010

HEALTHCHECK --interval=10s --timeout=5s --retries=5 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8010/api/mcp/v1/healthz').read()" || exit 1

CMD ["uvicorn", "mcp_registry.main:create_app", "--host", "0.0.0.0", "--port", "8010", "--factory"]
