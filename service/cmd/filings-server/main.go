// Command filings-server is the read-only HTTP service over the shared
// filings database. Configuration comes from the environment (see
// internal/config). The orchestrator (Python) owns writes and migrations;
// this process only serves reads.
package main

import (
	"context"
	"errors"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"syscall"
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
		// Drain the OTel export queues before the process exits. The
		// graceful shutdown path below cancels the server first, so this
		// defer runs after in-flight requests have ended and before
		// telemetry from those requests is lost.
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

	// Graceful shutdown. systemd sends SIGTERM on stop/restart and waits
	// TimeoutStopSec (default 90s) before SIGKILL. We listen for SIGTERM
	// and SIGINT, give the server up to 10 seconds to finish in-flight
	// requests, then return to main — letting the deferred OTel shutdown
	// drain the export queue cleanly.
	srv := &http.Server{
		Addr:    cfg.ListenAddr,
		Handler: handler,
	}

	sigCtx, stopNotifying := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stopNotifying()

	serverErr := make(chan error, 1)
	go func() {
		logger.Info("server starting", "addr", cfg.ListenAddr, "db_path", cfg.DBPath)
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			serverErr <- err
			return
		}
		serverErr <- nil
	}()

	select {
	case err := <-serverErr:
		if err != nil {
			logger.Error("server exited with error", "error", err)
			os.Exit(1)
		}
	case <-sigCtx.Done():
		logger.Info("shutdown signal received; draining in-flight requests")
		shutdownCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		if err := srv.Shutdown(shutdownCtx); err != nil {
			logger.Error("server shutdown error", "error", err)
		}
	}
}
