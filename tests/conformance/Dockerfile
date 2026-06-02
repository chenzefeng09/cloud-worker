FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir poetry

COPY pyproject.toml ./
RUN poetry config virtualenvs.create false && poetry install --no-dev --no-interaction --no-ansi

COPY . .

ENV BFF_URL=http://bff:8005
ENV ORCHESTRATOR_URL=http://session-orchestrator:8006
ENV SCHEDULER_URL=http://agent-scheduler:8007

CMD ["pytest", "-v", "--tb=short"]
