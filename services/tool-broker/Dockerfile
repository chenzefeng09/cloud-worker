# syntax=docker/dockerfile:1.7
#
# tool-broker. Reuse the session-orchestrator base image because the local dev
# environment already builds it with FastAPI/httpx and avoids network package
# installs during this image build.
FROM agent-platform-dev-session-orchestrator:latest

USER root
WORKDIR /app
RUN rm -rf /app/src /app/alembic /app/alembic.ini 2>/dev/null || true

COPY src/ ./src/
RUN chown -R app:app /app

ENV TOOL_BROKER_ENV=production \
    TOOL_BROKER_HTTP_PORT=8024 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

USER app
EXPOSE 8024

HEALTHCHECK --interval=10s --timeout=5s --retries=5 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8024/api/tool-broker/v1/healthz').read()" || exit 1

CMD ["uvicorn", "tool_broker.main:create_app", "--host", "0.0.0.0", "--port", "8024", "--factory"]
