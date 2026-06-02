"""OpenTelemetry init for agent-scheduler."""

import logging
import os

import opentelemetry.metrics
import opentelemetry.trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider


def init_observability(*, service_name: str) -> None:
    if os.environ.get("SCHEDULER_OTEL_ENABLED", "true").lower() == "false":
        return

    logging.basicConfig(
        level=logging.INFO,
        format='{"timestamp":"%(asctime)s","level":"%(levelname)s","service":"%(name)s","message":"%(message)s"}',
    )

    resource = Resource.create({SERVICE_NAME: service_name})
    opentelemetry.trace.set_tracer_provider(TracerProvider(resource=resource))
    opentelemetry.metrics.set_meter_provider(MeterProvider(resource=resource))
