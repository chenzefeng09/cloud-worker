# Use BuildKit's bundled Dockerfile frontend.  A remote ``# syntax=`` image
# lookup makes otherwise-local/offline rebuilds fail before the first layer.
# See services/bff/Dockerfile for the rationale on the ``foundation_sdk``
# additional context.
FROM python:3.12-slim

WORKDIR /app

RUN groupadd --system --gid 1000 app \
 && useradd  --system --uid 1000 --gid app --create-home --shell /usr/sbin/nologin app

# Poetry 2.x — required by the lockfile format committed alongside this Dockerfile.
RUN pip install --no-cache-dir poetry==2.1.4

COPY --from=foundation_sdk . /sdk
RUN pip install --no-cache-dir "/sdk[fastapi]"

COPY --from=feature_flag_client . /ff-client
RUN pip install --no-cache-dir "/ff-client[fastapi]"

COPY pyproject.toml poetry.lock* ./
# Pre-install the attachment/S3 runtime from the checked-in wheelhouse before
# Poetry evaluates the lockfile.  The directory contains both arm64 and x86_64
# wheels for the binary packages; installing by distribution name lets pip pick
# the wheel compatible with the current builder instead of trying every *.whl.
COPY wheels/ /tmp/wheels/
RUN pip install --no-cache-dir --no-index --find-links=/tmp/wheels --no-deps \
      "boto3==1.43.20" \
      "botocore==1.43.20" \
      "jmespath==1.1.0" \
      "python-dateutil==2.9.0.post0" \
      "python-multipart==0.0.30" \
      "redis==8.0.0" \
      "s3transfer==0.18.0" \
      "six==1.17.0" \
      "urllib3==2.7.0" \
      "pypdf==6.14.2" \
      "python-docx==1.2.0" \
      "openpyxl==3.1.5" \
      "python-pptx==1.0.2" \
      "lxml==6.1.1" \
      "Pillow==12.3.0" \
      "et-xmlfile==2.0.0" \
      "typing-extensions==4.15.0" \
      "XlsxWriter==3.2.9"
RUN poetry config virtualenvs.create false \
 && poetry install --no-root --without dev --no-interaction --no-ansi

RUN rm -rf /tmp/wheels

COPY src/ ./src/
COPY migrations/ ./migrations/
COPY alembic.ini .
RUN chown -R app:app /app

ENV SESSION_ORCHESTRATOR_ENV=production \
    SESSION_ORCHESTRATOR_HTTP_PORT=8006 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

USER app
EXPOSE 8006

HEALTHCHECK --interval=10s --timeout=5s --retries=5 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8006/healthz').read()" || exit 1

CMD ["uvicorn", "agent_session_orchestrator.main:create_app", "--host", "0.0.0.0", "--port", "8006", "--factory"]
