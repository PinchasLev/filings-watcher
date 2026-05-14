package store_test

import (
	"context"
	"database/sql"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	_ "modernc.org/sqlite"

	"github.com/PinchasLev/filings-watcher/service/internal/store"
)

// migrationsDir resolves the shared db/migrations directory used by both
// the Python orchestrator and these Go tests. Single source of schema truth.
func migrationsDir(t *testing.T) string {
	t.Helper()
	// service/internal/store/store_test.go → ../../../orchestrator/db/migrations
	dir, err := filepath.Abs(filepath.Join("..", "..", "..", "orchestrator", "db", "migrations"))
	if err != nil {
		t.Fatalf("resolve migrations dir: %v", err)
	}
	if _, err := os.Stat(dir); err != nil {
		t.Fatalf("migrations dir missing: %v", err)
	}
	return dir
}

// applyMigrations applies every .sql file in dir against db, in alphabetical
// order, stripping -- line comments and splitting on `;` — same behavior
// as the Python migration runner.
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
				t.Fatalf("exec %s: %v\nstmt: %s", f, err, stmt)
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
	cleaned := strings.Join(lines, "\n")
	var out []string
	for _, raw := range strings.Split(cleaned, ";") {
		stmt := strings.TrimSpace(raw)
		if stmt != "" {
			out = append(out, stmt)
		}
	}
	return out
}

// seedFilingAndClassifications inserts a small fixture: one filing with
// two classifications under one classifier_version.
func seedFilingAndClassifications(t *testing.T, db *sql.DB) {
	t.Helper()
	const filingInsert = `
		INSERT INTO filings (accession_number, cik, ticker, company_name, form,
			filing_date, primary_document, primary_document_url, items_json, fetched_at)
		VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
	`
	if _, err := db.Exec(filingInsert,
		"0001-26-001", "0000000001", "AAPL", "Apple Inc.", "8-K",
		"2026-04-30", "aapl.htm", "https://www.sec.gov/aapl.htm",
		`[{"number":"2.02"}]`,
		time.Now().UTC().Format(time.RFC3339Nano),
	); err != nil {
		t.Fatalf("insert filing: %v", err)
	}

	const classInsert = `
		INSERT INTO classifications (accession_number, item_number, item_title,
			event_type, event_domain, is_material, confidence, reasoning,
			classifier_version, taxonomy_version, classified_at)
		VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
	`
	now := time.Now().UTC()
	if _, err := db.Exec(classInsert,
		"0001-26-001", "2.02", "Results of Operations",
		"earnings_release", "financial", 1, 0.98, "Earnings.",
		"haiku-4.5+prompt-aaaa1111", "v1", now.Format(time.RFC3339Nano),
	); err != nil {
		t.Fatalf("insert classification 1: %v", err)
	}
	if _, err := db.Exec(classInsert,
		"0001-26-001", "5.02", "Departure",
		"exec_departure", "governance", 1, 0.92, "CFO resigned.",
		"haiku-4.5+prompt-aaaa1111", "v1", now.Add(time.Second).Format(time.RFC3339Nano),
	); err != nil {
		t.Fatalf("insert classification 2: %v", err)
	}
}

// freshDBPath creates an empty DB at a temp path with schema applied and
// returns the path. Returns both the path and the raw *sql.DB still open,
// so the caller can seed/append more data before closing.
func freshDBPath(t *testing.T) (string, *sql.DB) {
	t.Helper()
	dbPath := filepath.Join(t.TempDir(), "test.db")
	rawDB, err := sql.Open("sqlite", dbPath)
	if err != nil {
		t.Fatalf("open raw db: %v", err)
	}
	applyMigrations(t, rawDB, migrationsDir(t))
	seedFilingAndClassifications(t, rawDB)
	return dbPath, rawDB
}

// openStore opens a Store at the given path and registers cleanup.
func openStore(t *testing.T, dbPath string) store.Store {
	t.Helper()
	s, err := store.Open(dbPath)
	if err != nil {
		t.Fatalf("open store: %v", err)
	}
	t.Cleanup(func() { _ = s.Close() })
	return s
}

func TestLatestClassifications_ReturnsSeededRowsWithFilingFields(t *testing.T) {
	dbPath, raw := freshDBPath(t)
	_ = raw.Close()
	s := openStore(t, dbPath)

	rows, total, err := s.LatestClassifications(context.Background(), 10, 0)
	if err != nil {
		t.Fatalf("LatestClassifications: %v", err)
	}
	if total != 2 {
		t.Fatalf("total = %d, want 2", total)
	}
	if len(rows) != 2 {
		t.Fatalf("rows = %d, want 2", len(rows))
	}
	// Newest first by classified_at — the 5.02 row was inserted +1s after 2.02.
	if rows[0].EventType != "exec_departure" {
		t.Errorf("first row event_type = %q, want exec_departure", rows[0].EventType)
	}
	if rows[0].CompanyName != "Apple Inc." {
		t.Errorf("first row company_name = %q, want Apple Inc.", rows[0].CompanyName)
	}
	if rows[0].Ticker == nil || *rows[0].Ticker != "AAPL" {
		t.Errorf("ticker mismatch: %+v", rows[0].Ticker)
	}
}

func TestLatestClassifications_RespectsLimitAndOffset(t *testing.T) {
	dbPath, raw := freshDBPath(t)
	_ = raw.Close()
	s := openStore(t, dbPath)

	page1, _, err := s.LatestClassifications(context.Background(), 1, 0)
	if err != nil {
		t.Fatalf("page 1: %v", err)
	}
	page2, _, err := s.LatestClassifications(context.Background(), 1, 1)
	if err != nil {
		t.Fatalf("page 2: %v", err)
	}
	if len(page1) != 1 || len(page2) != 1 {
		t.Fatalf("page sizes = %d/%d, want 1/1", len(page1), len(page2))
	}
	if page1[0].ID == page2[0].ID {
		t.Errorf("pages returned the same row: id %d", page1[0].ID)
	}
}

func TestLatestClassifications_PicksMostRecentPerKey(t *testing.T) {
	// Two classifications for the same (accession, item, classifier_version)
	// — second written later. The UNIQUE INDEX should reject this insert,
	// which is exactly ADR 0011's append-only / no-duplicate guarantee.
	_, raw := freshDBPath(t)
	defer raw.Close()

	const classInsert = `
		INSERT INTO classifications (accession_number, item_number, item_title,
			event_type, event_domain, is_material, confidence, reasoning,
			classifier_version, taxonomy_version, classified_at)
		VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
	`
	if _, err := raw.Exec(classInsert,
		"0001-26-001", "2.02", "Results of Operations",
		"earnings_release", "financial", 1, 0.98, "Earnings (re-run).",
		"haiku-4.5+prompt-aaaa1111", "v1",
		time.Now().UTC().Add(time.Hour).Format(time.RFC3339Nano),
	); err != nil {
		// The UNIQUE INDEX should reject this insert — that's exactly the
		// idempotence guarantee ADR 0011 commits to. Confirm the rejection.
		if !strings.Contains(err.Error(), "UNIQUE") {
			t.Fatalf("expected UNIQUE constraint rejection, got: %v", err)
		}
		return
	}
	t.Fatal("expected UNIQUE constraint to reject the duplicate insert, but it succeeded")
}

func TestFilingByAccession_ReturnsFilingAndClassifications(t *testing.T) {
	dbPath, raw := freshDBPath(t)
	_ = raw.Close()
	s := openStore(t, dbPath)

	detail, err := s.FilingByAccession(context.Background(), "0001-26-001")
	if err != nil {
		t.Fatalf("FilingByAccession: %v", err)
	}
	if detail.Filing.AccessionNumber != "0001-26-001" {
		t.Errorf("accession = %q, want 0001-26-001", detail.Filing.AccessionNumber)
	}
	if detail.Filing.CompanyName != "Apple Inc." {
		t.Errorf("company = %q, want Apple Inc.", detail.Filing.CompanyName)
	}
	if len(detail.Classifications) != 2 {
		t.Errorf("classifications = %d, want 2", len(detail.Classifications))
	}
}

func TestFilingByAccession_NotFoundReturnsErrNotFound(t *testing.T) {
	dbPath, raw := freshDBPath(t)
	_ = raw.Close()
	s := openStore(t, dbPath)

	_, err := s.FilingByAccession(context.Background(), "does-not-exist")
	if !errors.Is(err, store.ErrNotFound) {
		t.Fatalf("expected ErrNotFound, got: %v", err)
	}
}
