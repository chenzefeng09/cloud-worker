# syntax=docker/dockerfile:1.7
#
# web-fetch-gateway.  We pin FROM agent-platform-dev-session-orchestrator
# because Docker Desktop on this host corrupts pypi metadata in the build
# network (bm2-bm3 fix-report) and the session-orchestrator image already
# carries the full FastAPI / httpx stack we need.  No wheels to copy because
# httpx is already present in the base image.
FROM agent-platform-dev-session-orchestrator:latest

USER root
WORKDIR /app
RUN rm -rf /app/src /app/alembic /app/alembic.ini 2>/dev/null || true

COPY src/ ./src/
RUN chown -R app:app /app

ENV WEB_FETCH_ENV=production \
    WEB_FETCH_HTTP_PORT=8013 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

USER app
EXPOSE 8013

HEALTHCHECK --interval=10s --timeout=5s --retries=5 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8013/api/web-fetch/v1/healthz').read()" || exit 1

CMD ["uvicorn", "web_fetch_gateway.main:create_app", "--host", "0.0.0.0", "--port", "8013", "--factory"]
