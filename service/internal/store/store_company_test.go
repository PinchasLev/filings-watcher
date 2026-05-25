package store_test

import (
	"context"
	"database/sql"
	"errors"
	"testing"
	"time"

	"github.com/PinchasLev/filings-watcher/service/internal/store"
)

// insertCikTicker adds a row to the cik_tickers mirror (not seeded by the
// shared fixture, so each test opts in to the identity it needs).
func insertCikTicker(t *testing.T, db *sql.DB, cik, ticker, name string) {
	t.Helper()
	_, err := db.Exec(
		`INSERT INTO cik_tickers (cik, ticker, company_name, updated_at) VALUES (?, ?, ?, ?)`,
		cik, ticker, name, time.Now().UTC().Format(time.RFC3339Nano),
	)
	if err != nil {
		t.Fatalf("insert cik_ticker: %v", err)
	}
}

func TestLookupCIKByTicker_CaseInsensitiveExactMatch(t *testing.T) {
	dbPath, raw := freshDBPath(t)
	insertCikTicker(t, raw, "0000000001", "AAPL", "Apple Inc.")
	_ = raw.Close()
	s := openStore(t, dbPath)

	// Lowercase input must still resolve — the store uppercases before matching.
	cik, err := s.LookupCIKByTicker(context.Background(), "aapl")
	if err != nil {
		t.Fatalf("LookupCIKByTicker: %v", err)
	}
	if cik != "0000000001" {
		t.Errorf("cik = %q, want 0000000001", cik)
	}
}

func TestLookupCIKByTicker_UnknownReturnsErrNotFound(t *testing.T) {
	dbPath, raw := freshDBPath(t)
	_ = raw.Close()
	s := openStore(t, dbPath)

	_, err := s.LookupCIKByTicker(context.Background(), "ZZZZ")
	if !errors.Is(err, store.ErrNotFound) {
		t.Fatalf("expected ErrNotFound, got: %v", err)
	}
}

func TestCompanyByCIK_PrefersCanonicalIdentityAndReturnsFilings(t *testing.T) {
	dbPath, raw := freshDBPath(t)
	// Canonical name deliberately differs from the as-filed "Apple Inc." so
	// the test proves the cik_tickers identity wins when present.
	insertCikTicker(t, raw, "0000000001", "AAPL", "Apple Inc. (canonical)")
	_ = raw.Close()
	s := openStore(t, dbPath)

	company, filings, total, err := s.CompanyByCIK(context.Background(), "0000000001", 50, 0)
	if err != nil {
		t.Fatalf("CompanyByCIK: %v", err)
	}
	if company.CompanyName != "Apple Inc. (canonical)" {
		t.Errorf("company name = %q, want canonical name from cik_tickers", company.CompanyName)
	}
	if company.Ticker != "AAPL" {
		t.Errorf("ticker = %q, want AAPL", company.Ticker)
	}
	if total != 2 {
		t.Errorf("total = %d, want 2 (both seeded classifications are material)", total)
	}
	if len(filings) != 2 {
		t.Errorf("filings = %d, want 2", len(filings))
	}
}

func TestCompanyByCIK_FallsBackToAsFiledIdentity(t *testing.T) {
	dbPath, raw := freshDBPath(t)
	// No cik_tickers row: a filer absent from SEC's ticker file. Identity
	// must fall back to the most recent filing's as-filed name/ticker.
	_ = raw.Close()
	s := openStore(t, dbPath)

	company, _, total, err := s.CompanyByCIK(context.Background(), "0000000001", 50, 0)
	if err != nil {
		t.Fatalf("CompanyByCIK: %v", err)
	}
	if company.CompanyName != "Apple Inc." {
		t.Errorf("company name = %q, want as-filed Apple Inc.", company.CompanyName)
	}
	if company.Ticker != "AAPL" {
		t.Errorf("ticker = %q, want as-filed AAPL", company.Ticker)
	}
	if total != 2 {
		t.Errorf("total = %d, want 2", total)
	}
}

func TestCompanyByCIK_UnknownCIKReturnsErrNotFound(t *testing.T) {
	dbPath, raw := freshDBPath(t)
	_ = raw.Close()
	s := openStore(t, dbPath)

	_, _, _, err := s.CompanyByCIK(context.Background(), "9999999999", 50, 0)
	if !errors.Is(err, store.ErrNotFound) {
		t.Fatalf("expected ErrNotFound for unknown CIK, got: %v", err)
	}
}

func TestCompanyByCIK_KnownInMirrorButNoFilings(t *testing.T) {
	dbPath, raw := freshDBPath(t)
	// In SEC's ticker file but we've classified nothing for it yet.
	insertCikTicker(t, raw, "0000000999", "NEWCO", "Newco Holdings")
	_ = raw.Close()
	s := openStore(t, dbPath)

	company, filings, total, err := s.CompanyByCIK(context.Background(), "0000000999", 50, 0)
	if err != nil {
		t.Fatalf("CompanyByCIK: %v", err)
	}
	if company.CompanyName != "Newco Holdings" {
		t.Errorf("company name = %q, want Newco Holdings", company.CompanyName)
	}
	if total != 0 || len(filings) != 0 {
		t.Errorf("total/filings = %d/%d, want 0/0 (tracked but nothing classified)", total, len(filings))
	}
}
