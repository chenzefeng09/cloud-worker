# syntax=docker/dockerfile:1.7
# cron-runner — self-contained build.
#
# Deliberately NOT derived from agent-platform-dev-session-orchestrator like
# skill-store/mcp-registry: that base is maintained via Dockerfile.overlay
# (image FROM itself), so its layer chain grows on every rebuild (700+ layers
# by 2026-06) and derived builds die with "mount options is too long" at the
# first RUN that touches /app.  cron-runner's dependency set is small enough
# to install directly; if local PyPI flakes (the historical reason for the
# shared base), retry the build.
FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir \
        "fastapi>=0.110" \
        "uvicorn[standard]>=0.29" \
        "sqlalchemy[asyncio]>=2.0" \
        "asyncpg>=0.29" \
        "pydantic-settings>=2.2" \
        "alembic>=1.13" \
        "apscheduler>=3.10,<4" \
        "httpx>=0.27" \
    && adduser --disabled-password --gecos "" app

COPY src/ ./src/
COPY alembic/ ./alembic/
COPY alembic.ini .
RUN chown -R app:app /app

ENV CRON_RUNNER_ENV=production \
    CRON_RUNNER_HTTP_PORT=8013 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

USER app
EXPOSE 8013

HEALTHCHECK --interval=10s --timeout=5s --retries=5 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8013/api/cron/v1/healthz').read()" || exit 1

CMD ["uvicorn", "cron_runner.main:create_app", "--host", "0.0.0.0", "--port", "8013", "--factory"]
