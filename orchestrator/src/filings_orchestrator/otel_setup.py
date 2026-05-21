"""OpenTelemetry SDK initialization for the orchestrator.

Configures the global TracerProvider and MeterProvider with an OTLP gRPC
exporter pointed at the host-local Collector (see ADR 0018). All
configuration is read from standard OTel environment variables so
operators can change endpoints, resource attributes, or service identity
without touching code.

Environment variables consumed:

- ``OTEL_EXPORTER_OTLP_ENDPOINT`` — gRPC URL of the Collector. **Required:**
  if unset, this module is a no-op; the SDK's default NoOp providers stay
  in place and no spans or metrics are emitted. This keeps tests and
  local-development runs quiet without needing a Collector on hand.
- ``OTEL_SERVICE_NAME`` — populates ``service.name`` resource attribute.
- ``OTEL_RESOURCE_ATTRIBUTES`` — comma-separated ``key=value`` pairs
  merged into the resource (e.g., ``service.version=<sha>``).

The systemd wrapper script that invokes ``scan-daily-index`` sets all
three (see ``infra/ssm_install_orchestrate_timer.tf``).

Idempotent: a second call within the same process is a no-op so
testing or repeated entry points do not stack span processors.
"""

from __future__ import annotations

import atexit
import os

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

_initialized = False


def setup_otel() -> None:
    """Initialize the global TracerProvider and MeterProvider for the process.

    No-op when ``OTEL_EXPORTER_OTLP_ENDPOINT`` is unset (local development,
    tests) — the SDK's default NoOp providers remain in place so ``Tracer``
    and ``Meter`` operations stay cheap and silent.

    A short-lived process (the typical orchestrator tick is ~seconds to
    ~minutes) needs its final spans and metrics flushed before exit. We
    register the shutdown via ``atexit`` so an unhandled ``SystemExit`` or
    a normal return still drains the export queue.
    """
    global _initialized
    if _initialized:
        return
    if not os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        return

    resource = Resource.create()

    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(tracer_provider)

    meter_provider = MeterProvider(
        resource=resource,
        metric_readers=[PeriodicExportingMetricReader(OTLPMetricExporter())],
    )
    metrics.set_meter_provider(meter_provider)

    atexit.register(_shutdown, tracer_provider, meter_provider)
    _initialized = True


def _shutdown(tracer_provider: TracerProvider, meter_provider: MeterProvider) -> None:
    tracer_provider.shutdown()
    meter_provider.shutdown()
