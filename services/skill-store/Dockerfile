# syntax=docker/dockerfile:1.7
# B-M3 skill-store. See services/mcp-registry/Dockerfile for the rationale —
# we pin FROM agent-platform-dev-session-orchestrator because Docker Desktop
# corrupts pypi metadata in this build network.
FROM agent-platform-dev-session-orchestrator:latest

USER root
WORKDIR /app
RUN rm -rf /app/src /app/alembic /app/alembic.ini 2>/dev/null || true

COPY --from=feature_flag_client . /tmp/ff-client
RUN pip install --no-cache-dir "/tmp/ff-client[fastapi]" && rm -rf /tmp/ff-client

COPY wheels/ /tmp/wheels/
RUN pip install --no-cache-dir --no-deps /tmp/wheels/*.whl && rm -rf /tmp/wheels

COPY src/ ./src/
COPY alembic/ ./alembic/
COPY alembic.ini .
# Platform-global default skills: the seed script + vendored skill tarballs are
# baked in so the one-shot ``skill-store-seed`` sidecar (see compose) can seed
# them into the platform tenant on a fresh ``docker compose up`` — no dependency
# on the external skills repo at deploy time.
COPY scripts/ ./scripts/
COPY seed/ ./seed/
RUN chown -R app:app /app

ENV SKILL_STORE_ENV=production \
    SKILL_STORE_HTTP_PORT=8011 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

USER app
EXPOSE 8011

HEALTHCHECK --interval=10s --timeout=5s --retries=5 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8011/api/skills/v1/healthz').read()" || exit 1

CMD ["uvicorn", "skill_store.main:create_app", "--host", "0.0.0.0", "--port", "8011", "--factory"]
