"""Integration smoke test for setup_otel().

The default test suite leaves OTEL_EXPORTER_OTLP_ENDPOINT unset, so
setup_otel() takes its quiet no-op path under pytest. That means the
production code path — provider construction, OTLP exporter wiring,
and the LangChain auto-instrumentation — was never exercised in CI
before this test.

This file is the gate that exercises the real path. A third-party
instrumentation that breaks against a new transitive-dep version
(see the wrapt 2.x incompat that wedged the orchestrator after
PR #57) raises here at the instrument() call and fails CI — instead
of failing in production on the next tick.
"""

from __future__ import annotations

import pytest


def test_setup_otel_initializes_under_collector_env_vars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """setup_otel must return without raising when the Collector env vars are set.

    No real Collector is listening on the endpoint; the SDK's exporter
    starts a background channel lazily and will silently drop spans
    until shutdown. What we are testing here is that the synchronous
    setup path — TracerProvider, MeterProvider, and the LangChain
    instrumentor's instrument() call — completes cleanly.
    """
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:4317")
    monkeypatch.setenv("OTEL_SERVICE_NAME", "filings-orchestrator-test")
    monkeypatch.setenv("OTEL_RESOURCE_ATTRIBUTES", "service.version=test")

    import filings_orchestrator.otel_setup as otel_setup_mod

    # Force re-initialization so the test actually exercises setup logic
    # even if a prior test (or repeat run) already flipped the guard.
    monkeypatch.setattr(otel_setup_mod, "_initialized", False)

    # The bare assertion of this test is "this does not raise."
    otel_setup_mod.setup_otel()
