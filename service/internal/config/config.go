// Package config reads runtime configuration from environment variables.
//
// The service is intentionally narrow at v0: a database path to read from
// and an address to listen on. Everything else lives in source.
package config

import (
	"errors"
	"fmt"
	"os"
)

// Config holds resolved runtime configuration.
type Config struct {
	// DBPath is the SQLite database file the orchestrator writes to and the
	// service reads from. Required.
	DBPath string

	// ListenAddr is the host:port the HTTP server binds to.
	// Defaults to ":8080" when unset.
	ListenAddr string
}

// FromEnv loads configuration from process environment variables.
// Returns an error when required values are missing or invalid.
func FromEnv() (Config, error) {
	dbPath := os.Getenv("FILINGS_DB_PATH")
	if dbPath == "" {
		return Config{}, errors.New("FILINGS_DB_PATH must be set")
	}

	listenAddr := os.Getenv("LISTEN_ADDR")
	if listenAddr == "" {
		listenAddr = ":8080"
	}

	if _, err := os.Stat(dbPath); err != nil {
		return Config{}, fmt.Errorf("FILINGS_DB_PATH %q: %w", dbPath, err)
	}

	return Config{
		DBPath:     dbPath,
		ListenAddr: listenAddr,
	}, nil
}
