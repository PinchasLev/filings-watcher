package alerts_test

import (
	"bytes"
	"context"
	"database/sql"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"testing"

	_ "modernc.org/sqlite"

	"github.com/PinchasLev/filings-watcher/service/internal/alerts"
)

// migrationsDir resolves the shared schema (single source of truth, same dir
// the Python runner and the store tests use).
func migrationsDir(t *testing.T) string {
	t.Helper()
	// service/internal/alerts → ../../../orchestrator/db/migrations
	dir, err := filepath.Abs(filepath.Join("..", "..", "..", "orchestrator", "db", "migrations"))
	if err != nil {
		t.Fatalf("resolve migrations dir: %v", err)
	}
	if _, err := os.Stat(dir); err != nil {
		t.Fatalf("migrations dir missing: %v", err)
	}
	return dir
}

func applyMigrations(t *testing.T, db *sql.DB, dir string) {
	t.Helper()
	files, err := filepath.Glob(filepath.Join(dir, "*.sql"))
	if err != nil {
		t.Fatalf("glob migrations: %v", err)
	}
	for _, f := range files {
		raw, err := os.ReadFile(f)
		if err != nil {
			t.Fatalf("read %s: %v", f, err)
		}
		for _, stmt := range splitStatements(string(raw)) {
			if _, err := db.Exec(stmt); err != nil {
				t.Fatalf("exec %s: %v", f, err)
			}
		}
	}
}

func splitStatements(sqlText string) []string {
	var lines []string
	for _, line := range strings.Split(sqlText, "\n") {
		if i := strings.Index(line, "--"); i >= 0 {
			line = line[:i]
		}
		lines = append(lines, line)
	}
	var out []string
	for _, raw := range strings.Split(strings.Join(lines, "\n"), ";") {
		if stmt := strings.TrimSpace(raw); stmt != "" {
			out = append(out, stmt)
		}
	}
	return out
}

// freshWriter returns an alerts.Writer over a temp DB with the schema applied,
// plus a separate raw handle for the test to query the resulting rows.
func freshWriter(t *testing.T, logger *slog.Logger) (*alerts.Writer, *sql.DB) {
	t.Helper()
	dbPath := filepath.Join(t.TempDir(), "test.db")
	raw, err := sql.Open("sqlite", dbPath)
	if err != nil {
		t.Fatalf("open raw db: %v", err)
	}
	applyMigrations(t, raw, migrationsDir(t))

	w, err := alerts.Open(dbPath, logger)
	if err != nil {
		t.Fatalf("open writer: %v", err)
	}
	t.Cleanup(func() { _ = w.Close(); _ = raw.Close() })
	return w, raw
}

func discardLogger() *slog.Logger {
	return slog.New(slog.NewTextHandler(io.Discard, nil))
}

func TestEmit_InsertsRowWithPayload(t *testing.T) {
	w, raw := freshWriter(t, discardLogger())
	w.Emit(context.Background(), alerts.SeverityAlert, "Handler panic recovered",
		alerts.WithBody("GET /live panicked: boom"),
		alerts.WithDedupKey("panic:/live"),
		alerts.WithFields(map[string]any{"method": "GET", "path": "/live"}),
	)

	var (
		severity, title, fieldsJSON string
		body, dedup                 sql.NullString
		delivered                   sql.NullString
	)
	err := raw.QueryRow(
		`SELECT severity, title, body, fields_json, dedup_key, delivered_at FROM alerts_outbox`,
	).Scan(&severity, &title, &body, &fieldsJSON, &dedup, &delivered)
	if err != nil {
		t.Fatalf("query row: %v", err)
	}
	if severity != alerts.SeverityAlert || title != "Handler panic recovered" {
		t.Fatalf("severity/title = %q/%q", severity, title)
	}
	if !body.Valid || body.String != "GET /live panicked: boom" {
		t.Fatalf("body = %+v", body)
	}
	if !dedup.Valid || dedup.String != "panic:/live" {
		t.Fatalf("dedup_key = %+v", dedup)
	}
	if !strings.Contains(fieldsJSON, `"method":"GET"`) || !strings.Contains(fieldsJSON, `"path":"/live"`) {
		t.Fatalf("fields_json = %s", fieldsJSON)
	}
	if delivered.Valid {
		t.Fatalf("new row should be undelivered, got delivered_at = %q", delivered.String)
	}
}

func TestEmit_DefaultsNullBodyAndDedup(t *testing.T) {
	w, raw := freshWriter(t, discardLogger())
	w.Emit(context.Background(), alerts.SeverityInfo, "Something informational")

	var body, dedup sql.NullString
	var fieldsJSON string
	if err := raw.QueryRow(
		`SELECT body, dedup_key, fields_json FROM alerts_outbox`,
	).Scan(&body, &dedup, &fieldsJSON); err != nil {
		t.Fatalf("query: %v", err)
	}
	if body.Valid {
		t.Fatalf("body should be NULL, got %q", body.String)
	}
	if dedup.Valid {
		t.Fatalf("dedup_key should be NULL, got %q", dedup.String)
	}
	if fieldsJSON != "{}" {
		t.Fatalf("fields_json should default to {}, got %s", fieldsJSON)
	}
}

func TestEmit_RejectsUnknownSeverity(t *testing.T) {
	var buf bytes.Buffer
	w, raw := freshWriter(t, slog.New(slog.NewTextHandler(&buf, nil)))
	w.Emit(context.Background(), "critical", "nope")

	var count int
	if err := raw.QueryRow(`SELECT COUNT(*) FROM alerts_outbox`).Scan(&count); err != nil {
		t.Fatalf("count: %v", err)
	}
	if count != 0 {
		t.Fatalf("unknown severity must not insert a row, got %d", count)
	}
	if !strings.Contains(buf.String(), "unknown severity") {
		t.Fatalf("expected a dropped-severity log, got: %s", buf.String())
	}
}

func TestEmit_BestEffortSwallowsWriteFailure(t *testing.T) {
	var buf bytes.Buffer
	w, _ := freshWriter(t, slog.New(slog.NewTextHandler(&buf, nil)))
	if err := w.Close(); err != nil {
		t.Fatalf("close: %v", err)
	}
	// Emitting against a closed writer must not panic; it logs and returns.
	w.Emit(context.Background(), alerts.SeverityAlert, "after close")
	if !strings.Contains(buf.String(), "alert emit failed") {
		t.Fatalf("expected an emit-failure log, got: %s", buf.String())
	}
}

func TestNop_Discards(t *testing.T) {
	// Compiles to the Emitter interface and never panics.
	var e alerts.Emitter = alerts.Nop{}
	e.Emit(context.Background(), alerts.SeverityAlert, "ignored", alerts.WithBody("x"))
}
