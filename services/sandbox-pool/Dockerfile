# syntax=docker/dockerfile:1.7
# See services/bff/Dockerfile for the rationale on the ``foundation_sdk``
# additional context.
FROM python:3.12-slim

WORKDIR /app

RUN groupadd --system --gid 1000 app \
 && useradd  --system --uid 1000 --gid app --create-home --shell /usr/sbin/nologin app

RUN pip install --no-cache-dir poetry==2.1.4

COPY --from=foundation_sdk . /sdk
RUN pip install --no-cache-dir "/sdk[fastapi]"

COPY pyproject.toml poetry.lock* ./
RUN poetry config virtualenvs.create false \
 && poetry install --no-root --without dev --no-interaction --no-ansi

COPY src/ ./src/
RUN chown -R app:app /app

ENV SANDBOX_POOL_ENV=production \
    SANDBOX_POOL_HTTP_PORT=8008 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

USER app
EXPOSE 8008

HEALTHCHECK --interval=10s --timeout=5s --retries=5 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8008/api/sandbox-pool/v1/healthz').read()" || exit 1

CMD ["uvicorn", "sandbox_pool.main:create_app", "--host", "0.0.0.0", "--port", "8008", "--factory"]
