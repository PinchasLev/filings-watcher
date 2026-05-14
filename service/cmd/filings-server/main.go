// Command filings-server is the read-only HTTP service over the shared
// filings database. Configuration comes from the environment (see
// internal/config). The orchestrator (Python) owns writes and migrations;
// this process only serves reads.
package main

import (
	"log/slog"
	"net/http"
	"os"

	"github.com/PinchasLev/filings-watcher/service/internal/config"
	"github.com/PinchasLev/filings-watcher/service/internal/server"
	"github.com/PinchasLev/filings-watcher/service/internal/store"
)

func main() {
	logger := slog.New(slog.NewJSONHandler(os.Stdout, nil))
	slog.SetDefault(logger)

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

	handler := server.New(s)
	logger.Info("server starting", "addr", cfg.ListenAddr, "db_path", cfg.DBPath)
	if err := http.ListenAndServe(cfg.ListenAddr, handler); err != nil {
		logger.Error("server exited with error", "error", err)
		os.Exit(1)
	}
}
