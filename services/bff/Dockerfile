# syntax=docker/dockerfile:1.7
# Run as the unprivileged ``app`` user — saas-foundation CONVENTIONS.md §12.
# The foundation SDK is brought in via a docker compose ``additional_contexts``
# entry called ``foundation_sdk`` (see deploy/docker-compose/agent-platform.yml);
# this Dockerfile copies the SDK source into the image and installs it as a
# regular wheel.  Until the SDK is published to a private PyPI, this is the
# least-friction way to keep the dependency reproducible across local-dev,
# CI, and production builds without polluting the primary build context.
FROM python:3.12-slim

WORKDIR /app

RUN groupadd --system --gid 1000 app \
 && useradd  --system --uid 1000 --gid app --create-home --shell /usr/sbin/nologin app

# Poetry 2.x — required by the lockfile format committed alongside this Dockerfile.
RUN pip install --no-cache-dir poetry==2.1.4

# Install the foundation SDK first so changes to the service code don't bust
# the SDK install layer cache.
COPY --from=foundation_sdk . /sdk
RUN pip install --no-cache-dir "/sdk[fastapi]"

COPY pyproject.toml poetry.lock* ./
RUN poetry config virtualenvs.create false \
 && poetry install --no-root --without dev --no-interaction --no-ansi

COPY src/ ./src/
RUN chown -R app:app /app

ENV BFF_ENV=production \
    BFF_HTTP_PORT=8005 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

USER app
EXPOSE 8005

HEALTHCHECK --interval=10s --timeout=5s --retries=5 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8005/healthz').read()" || exit 1

CMD ["uvicorn", "agent_bff.main:create_app", "--host", "0.0.0.0", "--port", "8005", "--factory"]
