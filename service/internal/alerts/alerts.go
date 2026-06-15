// Package alerts lets the service raise operator alerts by appending rows to
// the shared alerts_outbox table (ADR 0031).
//
// The "Python is the only writer" rule is scoped to the product/classifier
// tables and still holds — this package never touches those. alerts_outbox is
// an auxiliary operational table both processes may write; a standalone
// alarm-drain CLI is the only consumer and the only Discord-aware component, so
// emitting here carries no transport knowledge. The writer is deliberately
// separate from the read-only store.Store: the read path keeps its "only
// SELECT" clarity; alerts get their own write handle alongside it.
//
// Emit is best-effort. A failed write is logged and swallowed, never
// propagated — an alert about a problem must not become a second problem
// (crash a request, or mask the panic it was reporting).
package alerts

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"log/slog"
	"time"

	_ "modernc.org/sqlite"
)

// Severity values mirror the Python emitter and double as the drainer's
// channel-routing discriminator: the emitter states a level, the downstream
// drainer decides what to do with it (ADR 0031).
const (
	SeverityAlert = "alert"
	SeverityInfo  = "info"
)

// Emitter is the narrow capability the rest of the service depends on: raise
// one alert. An interface so middleware/handlers can be tested with a fake and
// so a no-op (Nop) is a drop-in when alerting is unavailable.
type Emitter interface {
	Emit(ctx context.Context, severity, title string, opts ...Option)
}

// emitOptions carries the optional payload an alert may add beyond severity and
// title. Mirrors the Python emit_alert keyword args.
type emitOptions struct {
	body     string
	dedupKey string
	fields   map[string]any
}

// Option configures an Emit call (functional-options pattern).
type Option func(*emitOptions)

// WithBody sets the longer human-facing detail line.
func WithBody(body string) Option { return func(o *emitOptions) { o.body = body } }

// WithDedupKey sets the drainer's coalescing key (empty = always deliver).
func WithDedupKey(key string) Option { return func(o *emitOptions) { o.dedupKey = key } }

// WithFields sets the structured context the drainer renders into the message.
func WithFields(fields map[string]any) Option {
	return func(o *emitOptions) { o.fields = fields }
}

// Writer is the SQLite-backed Emitter holding its own write handle to the
// shared DB.
type Writer struct {
	db     *sql.DB
	logger *slog.Logger
}

// Open opens a dedicated write handle for alert emission. The pool is capped at
// one connection (SQLite allows a single writer; this also makes the
// busy_timeout pragma stick) and busy_timeout lets a write wait out the brief
// WAL writer lock rather than fail immediately under concurrency with the
// orchestrator.
func Open(dbPath string, logger *slog.Logger) (*Writer, error) {
	db, err := sql.Open("sqlite", dbPath)
	if err != nil {
		return nil, fmt.Errorf("open alerts writer: %w", err)
	}
	db.SetMaxOpenConns(1)
	if _, err := db.Exec("PRAGMA busy_timeout = 5000"); err != nil {
		_ = db.Close()
		return nil, fmt.Errorf("set busy_timeout: %w", err)
	}
	if err := db.Ping(); err != nil {
		_ = db.Close()
		return nil, fmt.Errorf("ping alerts writer: %w", err)
	}
	return &Writer{db: db, logger: logger}, nil
}

// Close releases the write handle.
func (w *Writer) Close() error { return w.db.Close() }

// Emit appends one alert row. Best-effort: any failure is logged and
// swallowed, never returned. An invalid severity is dropped (logged), matching
// the Python emitter's guard.
func (w *Writer) Emit(ctx context.Context, severity, title string, opts ...Option) {
	if severity != SeverityAlert && severity != SeverityInfo {
		w.logger.Error("alert dropped: unknown severity", "severity", severity, "title", title)
		return
	}

	var o emitOptions
	for _, opt := range opts {
		opt(&o)
	}

	fieldsJSON := "{}"
	if len(o.fields) > 0 {
		b, err := json.Marshal(o.fields)
		if err != nil {
			w.logger.Error("alert dropped: marshal fields", "error", err, "title", title)
			return
		}
		fieldsJSON = string(b)
	}

	// NULL for empty body/dedup_key so the column reads as "unset" rather than
	// an empty string — matches the Python producer.
	var body, dedupKey any
	if o.body != "" {
		body = o.body
	}
	if o.dedupKey != "" {
		dedupKey = o.dedupKey
	}

	const q = `
		INSERT INTO alerts_outbox (created_at, severity, title, body, fields_json, dedup_key)
		VALUES (?, ?, ?, ?, ?, ?)
	`
	if _, err := w.db.ExecContext(
		ctx, q,
		time.Now().UTC().Format(time.RFC3339Nano),
		severity, title, body, fieldsJSON, dedupKey,
	); err != nil {
		w.logger.Error("alert emit failed", "error", err, "severity", severity, "title", title)
	}
}

// Nop is an Emitter that discards alerts. Used as the safe fallback when the
// alert writer cannot be opened — alerting is non-critical and must never
// prevent the service from serving reads.
type Nop struct{}

// Emit discards the alert.
func (Nop) Emit(context.Context, string, string, ...Option) {}
