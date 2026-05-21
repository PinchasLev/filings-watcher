// Package otel initializes the OpenTelemetry SDK for the filings-server
// service. Per ADR 0018, the application emits via OTLP gRPC to the
// host-local Collector; all configuration is read from standard OTel
// environment variables so operators control endpoints, resource
// attributes, and service identity without code changes.
//
// Environment variables consumed:
//
//   - OTEL_EXPORTER_OTLP_ENDPOINT: gRPC URL of the Collector. Required;
//     when unset, Setup is a no-op and the SDK's default NoOp providers
//     remain in place. This keeps local development and tests quiet.
//   - OTEL_SERVICE_NAME: populates the service.name resource attribute.
//   - OTEL_RESOURCE_ATTRIBUTES: comma-separated key=value pairs merged
//     into the resource (e.g. service.namespace=filings-watcher).
//
// The systemd unit at filings-server.service sets all three (see
// infra/user_data.sh.tpl).
package otel

import (
	"context"
	"fmt"
	"os"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/exporters/otlp/otlpmetric/otlpmetricgrpc"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracegrpc"
	"go.opentelemetry.io/otel/sdk/metric"
	"go.opentelemetry.io/otel/sdk/resource"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
)

// Setup initializes the global TracerProvider and MeterProvider, and
// returns a shutdown function that the caller should defer to drain
// the export queues before the process exits.
//
// When OTEL_EXPORTER_OTLP_ENDPOINT is unset, Setup is a no-op: it
// returns a shutdown function that does nothing and a nil error. The
// SDK's default NoOp providers remain in place; callers that obtain
// tracers or meters get cheap no-op implementations.
func Setup(ctx context.Context) (func(context.Context) error, error) {
	if os.Getenv("OTEL_EXPORTER_OTLP_ENDPOINT") == "" {
		return func(context.Context) error { return nil }, nil
	}

	res, err := resource.New(ctx, resource.WithFromEnv(), resource.WithTelemetrySDK())
	if err != nil {
		return nil, fmt.Errorf("otel resource: %w", err)
	}

	traceExporter, err := otlptracegrpc.New(ctx)
	if err != nil {
		return nil, fmt.Errorf("otel trace exporter: %w", err)
	}
	tracerProvider := sdktrace.NewTracerProvider(
		sdktrace.WithBatcher(traceExporter),
		sdktrace.WithResource(res),
	)
	otel.SetTracerProvider(tracerProvider)

	metricExporter, err := otlpmetricgrpc.New(ctx)
	if err != nil {
		return nil, fmt.Errorf("otel metric exporter: %w", err)
	}
	meterProvider := metric.NewMeterProvider(
		metric.WithReader(metric.NewPeriodicReader(metricExporter)),
		metric.WithResource(res),
	)
	otel.SetMeterProvider(meterProvider)

	shutdown := func(shutdownCtx context.Context) error {
		var firstErr error
		if err := tracerProvider.Shutdown(shutdownCtx); err != nil && firstErr == nil {
			firstErr = err
		}
		if err := meterProvider.Shutdown(shutdownCtx); err != nil && firstErr == nil {
			firstErr = err
		}
		return firstErr
	}
	return shutdown, nil
}
