// Integration smoke test for Setup().
//
// The default test environment leaves OTEL_EXPORTER_OTLP_ENDPOINT unset,
// so Setup() takes its quiet no-op path under `go test`. That means the
// production code path — TracerProvider construction, OTLP gRPC
// exporter wiring, MeterProvider construction — is never exercised
// without this gate.
//
// This file is the gate that exercises the real path. A third-party
// instrumentation that breaks against a new transitive-dep version
// (the kind of compat issue captured in the orchestrator at
// integration-test-third-party-instrumentation) raises here at Setup
// time and fails CI — instead of failing on the host on the first
// real request.
package otel

import (
	"context"
	"testing"
	"time"
)

func TestSetupNoopWithoutEndpoint(t *testing.T) {
	t.Setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "")
	shutdown, err := Setup(context.Background())
	if err != nil {
		t.Fatalf("Setup returned error on the no-op path: %v", err)
	}
	if shutdown == nil {
		t.Fatal("Setup returned nil shutdown function on the no-op path")
	}
	if err := shutdown(context.Background()); err != nil {
		t.Fatalf("no-op shutdown returned error: %v", err)
	}
}

func TestSetupInitializesUnderCollectorEnvVars(t *testing.T) {
	t.Setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "127.0.0.1:4317")
	t.Setenv("OTEL_SERVICE_NAME", "filings-server-test")
	t.Setenv("OTEL_RESOURCE_ATTRIBUTES", "service.version=test")

	shutdown, err := Setup(context.Background())
	if err != nil {
		t.Fatalf("Setup returned error under Collector env vars: %v", err)
	}
	if shutdown == nil {
		t.Fatal("Setup returned nil shutdown function under Collector env vars")
	}

	// Drain with a short context — the OTLP exporter will fail to reach
	// the (non-existent) Collector endpoint and may retry, so an unbounded
	// context would let the test hang for the default retry budget. The
	// shape we're verifying is "shutdown returns within the deadline,"
	// not that the bytes actually went anywhere.
	shutdownCtx, cancel := context.WithTimeout(context.Background(), 500*time.Millisecond)
	defer cancel()
	if err := shutdown(shutdownCtx); err != nil {
		t.Logf("shutdown returned non-fatal error (expected, no Collector listening): %v", err)
	}
}
