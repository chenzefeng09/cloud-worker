"""OTel init for BFF."""

import os

import opentelemetry.metrics
import opentelemetry.trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider


def init_observability(*, service_name: str) -> None:
    # Logging is set up by create_app (logging_setup), whether OTel is on or not.
    if os.environ.get("BFF_OTEL_ENABLED", "true").lower() == "false":
        return

    resource = Resource.create({SERVICE_NAME: service_name})
    opentelemetry.trace.set_tracer_provider(TracerProvider(resource=resource))
    opentelemetry.metrics.set_meter_provider(MeterProvider(resource=resource))
