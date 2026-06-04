"""OpenTelemetry initialization — one-liner for each service."""

import logging
import os


def init_observability(*, service_name: str, otlp_endpoint: str | None = None) -> None:
    """Bootstrap OTel SDK for traces, metrics, and structured logging."""
    if os.environ.get("SESSION_ORCHESTRATOR_OTEL_ENABLED", "true").lower() == "false":
        return

    _ = otlp_endpoint or os.environ.get(
        "SESSION_ORCHESTRATOR_OTEL_EXPORTER_ENDPOINT", "http://localhost:4317"
    )

    # Structured logging setup
    logging.basicConfig(
        level=logging.INFO,
        format='{"timestamp":"%(asctime)s","level":"%(levelname)s","service":"%(name)s","message":"%(message)s"}',
    )

    # OTel SDK auto-instrumentation is handled at the FastAPI level via
    # opentelemetry-instrumentation-fastapi in production.
    # In dev, we keep it lightweight.
    import opentelemetry.metrics
    import opentelemetry.trace
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.resources import SERVICE_NAME, Resource
    from opentelemetry.sdk.trace import TracerProvider

    resource = Resource.create({SERVICE_NAME: service_name})
    opentelemetry.trace.set_tracer_provider(TracerProvider(resource=resource))
    opentelemetry.metrics.set_meter_provider(MeterProvider(resource=resource))
