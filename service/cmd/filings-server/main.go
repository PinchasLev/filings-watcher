// Command filings-server is the read-only HTTP service over the shared
// filings database. Configuration comes from the environment (see
// internal/config). The orchestrator (Python) owns writes and migrations;
// this process only serves reads.
package main

import (
	"context"
	"log/slog"
	"net/http"
	"os"
	"time"

	"go.opentelemetry.io/contrib/instrumentation/net/http/otelhttp"

	"github.com/PinchasLev/filings-watcher/service/internal/config"
	otelsetup "github.com/PinchasLev/filings-watcher/service/internal/otel"
	"github.com/PinchasLev/filings-watcher/service/internal/server"
	"github.com/PinchasLev/filings-watcher/service/internal/store"
)

func main() {
	logger := slog.New(slog.NewJSONHandler(os.Stdout, nil))
	slog.SetDefault(logger)

	shutdown, err := otelsetup.Setup(context.Background())
	if err != nil {
		logger.Error("otel setup failed", "error", err)
		os.Exit(1)
	}
	defer func() {
		// Best-effort flush. http.ListenAndServe does not return on
		// SIGTERM today, so this defer fires only on graceful exit paths.
		// The BatchSpanProcessor and PeriodicReader bound steady-state
		// span/metric loss; the small remainder is acceptable for v0.
		// Graceful HTTP shutdown is tracked as a small follow-up.
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		if err := shutdown(ctx); err != nil {
			logger.Error("otel shutdown failed", "error", err)
		}
	}()

	cfg, err := config.FromEnv()
	if err != nil {
		logger.Error("config load failed", "error", err)
		os.Exit(1)
	}

	s, err := store.Open(cfg.DBPath)
	if err != nil {
		logger.Error("store open failed", "error", err, "db_path", cfg.DBPath)
		os.Exit(1)
	}
	defer func() {
		if err := s.Close(); err != nil {
			logger.Error("store close failed", "error", err)
		}
	}()

	// otelhttp wraps the mux so every request becomes a span with
	// http.* semantic-convention attributes (method, route, status code,
	// duration). The span name shown here is the surface name, not the
	// per-request name — the contrib package fills the route in.
	handler := otelhttp.NewHandler(server.New(s), "filings-server")
	logger.Info("server starting", "addr", cfg.ListenAddr, "db_path", cfg.DBPath)
	if err := http.ListenAndServe(cfg.ListenAddr, handler); err != nil {
		logger.Error("server exited with error", "error", err)
		os.Exit(1)
	}
}
