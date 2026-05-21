"""OpenTelemetry SDK initialization for the orchestrator.

Configures the global TracerProvider and MeterProvider with an OTLP gRPC
exporter pointed at the host-local Collector (see ADR 0018), then applies
the LangChain instrumentation so library-level spans (per-chain,
per-LLM-call, per-tool) nest naturally as children of the application's
own spans. All configuration is read from standard OTel environment
variables so operators can change endpoints, resource attributes, or
service identity without touching code.

Environment variables consumed:

- ``OTEL_EXPORTER_OTLP_ENDPOINT`` — gRPC URL of the Collector. **Required:**
  if unset, this module is a no-op; the SDK's default NoOp providers stay
  in place and no spans or metrics are emitted. This keeps tests and
  local-development runs quiet without needing a Collector on hand.
- ``OTEL_SERVICE_NAME`` — populates ``service.name`` resource attribute.
- ``OTEL_RESOURCE_ATTRIBUTES`` — comma-separated ``key=value`` pairs
  merged into the resource (e.g., ``service.version=<sha>``).
- ``TRACELOOP_TRACE_CONTENT`` — when set to ``false``, the LangChain
  instrumentation does not include prompt and completion content as
  span attributes. We set it false in the systemd wrapper: 8-K bodies
  inflate trace volume without adding signal we don't already have via
  LangSmith's deeper content capture.

The systemd wrapper script that invokes ``scan-daily-index`` sets all
four (see ``infra/ssm_install_orchestrate_timer.tf``).

Idempotent: a second call within the same process is a no-op so
testing or repeated entry points do not stack span processors. The OTel
SDK's TracerProvider and MeterProvider both default ``shutdown_on_exit=True``,
so they each register their own ``atexit`` handler at construction time —
the export queue is drained on process exit without further wiring here.
"""

from __future__ import annotations

import os

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.langchain import LangchainInstrumentor
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

    The LangChain instrumentation is applied after providers are registered
    so that library-emitted spans use the configured TracerProvider and
    nest under whatever span context is active at LangChain call sites.
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

    LangchainInstrumentor().instrument()

    _initialized = True
