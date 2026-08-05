# Use BuildKit's bundled Dockerfile frontend (no remote `# syntax=` lookup):
# a remote frontend image fetch makes otherwise-local rebuilds stall before the
# first layer on this host (Docker Hub unreachable/flaky). Mirrors the
# session-orchestrator Dockerfile.overlay stance. The directives used below are
# all standard and supported by the bundled frontend.
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

# Install SQLAlchemy + asyncpg + aiosqlite + alembic (not in the base image)
# 官方 PyPI：曾因本机 Docker Desktop 损坏 PyPI 元数据改走清华源，该问题已消失；
# 2026-07 起清华源反而从本机持续 SSL EOF 不可达，切回官方源（CN 云机部署时按需换回镜像源）
RUN pip install --no-cache-dir \
    "sqlalchemy[asyncio]>=2.0.36,<3" \
    "asyncpg>=0.30.0,<1" \
    "aiosqlite>=0.20.0,<1" \
    "alembic>=1.14.0,<2" \
    "python-jose[cryptography]>=3.3.0,<4"

COPY src/ ./src/
COPY alembic/ ./alembic/
COPY alembic.ini ./alembic.ini
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
