# syntax=docker/dockerfile:1.7
# B-M3 knowledge-ingest. See services/mcp-registry/Dockerfile for the
# rationale — we pin FROM agent-platform-dev-session-orchestrator because
# Docker Desktop corrupts pypi metadata in this build network.
#
# NOTE: pgvector + pypdf are NOT in the base image. The current build relies
# on the SQLite/JSON fallback paths in domain/types.py + parsers/base.py.
# When the build network is fixed, the proper Dockerfile (with `pip install
# pgvector pypdf`) can be restored.
FROM agent-platform-dev-session-orchestrator:latest

USER root
WORKDIR /app
RUN rm -rf /app/src /app/alembic /app/alembic.ini 2>/dev/null || true

COPY wheels/ /tmp/wheels/
RUN pip install --no-cache-dir --no-deps /tmp/wheels/*.whl && rm -rf /tmp/wheels

COPY src/ ./src/
COPY alembic/ ./alembic/
COPY alembic.ini .
RUN chown -R app:app /app

ENV KNOWLEDGE_INGEST_ENV=production \
    KNOWLEDGE_INGEST_HTTP_PORT=8012 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

USER app
EXPOSE 8012

HEALTHCHECK --interval=10s --timeout=5s --retries=5 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8012/api/knowledge/v1/healthz').read()" || exit 1

CMD ["uvicorn", "knowledge_ingest.main:create_app", "--host", "0.0.0.0", "--port", "8012", "--factory"]
