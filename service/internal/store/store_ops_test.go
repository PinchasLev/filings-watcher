package store_test

import (
	"context"
	"database/sql"
	"testing"
	"time"
)

// insertLLMCall seeds one row in the llm_calls table at the given
// emitted_at (ISO 8601). Stage and accession are set to plausible defaults.
func insertLLMCall(t *testing.T, db *sql.DB, emittedAt string, costUSD float64) {
	t.Helper()
	const q = `
		INSERT INTO llm_calls (emitted_at, model, stage, accession_number,
			input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens,
			estimated_cost_usd)
		VALUES (?, 'haiku', 'classify', '0001-26-001', 1000, 500, 0, 0, ?)
	`
	if _, err := db.Exec(q, emittedAt, costUSD); err != nil {
		t.Fatalf("insert llm_call: %v", err)
	}
}

// TestTodaySpend_SumsAndCountsRowsForTodayUTC checks the day boundary is UTC
// and that yesterday's rows don't leak into today's totals.
func TestTodaySpend_SumsAndCountsRowsForTodayUTC(t *testing.T) {
	dbPath, raw := freshDBPath(t)

	today := time.Now().UTC().Format("2006-01-02")
	yesterday := time.Now().UTC().AddDate(0, 0, -1).Format("2006-01-02")

	insertLLMCall(t, raw, today+"T10:00:00+00:00", 1.25)
	insertLLMCall(t, raw, today+"T14:00:00+00:00", 0.75)
	insertLLMCall(t, raw, yesterday+"T23:00:00+00:00", 99.00) // must not count

	_ = raw.Close()
	s := openStore(t, dbPath)

	snap, err := s.TodaySpend(context.Background())
	if err != nil {
		t.Fatalf("TodaySpend: %v", err)
	}
	if snap.CallCount != 2 {
		t.Errorf("CallCount = %d, want 2 (yesterday excluded)", snap.CallCount)
	}
	if delta := snap.TotalUSD - 2.00; delta < -0.001 || delta > 0.001 {
		t.Errorf("TotalUSD = %f, want 2.00", snap.TotalUSD)
	}
}

// TestTodaySpend_ReturnsZeroWhenEmpty confirms the COALESCE handles the
// fresh-DB case (no llm_calls rows at all).
func TestTodaySpend_ReturnsZeroWhenEmpty(t *testing.T) {
	dbPath, raw := freshDBPath(t)
	_ = raw.Close()
	s := openStore(t, dbPath)

	snap, err := s.TodaySpend(context.Background())
	if err != nil {
		t.Fatalf("TodaySpend: %v", err)
	}
	if snap.CallCount != 0 || snap.TotalUSD != 0.0 {
		t.Errorf("empty result = %+v, want zero values", snap)
	}
}

// TestAtomSnapshotFreshness_ReturnsLatestNonNull ignores the seeded
// daily-index-style filing (no submitted_at) and returns the explicit
// timestamp from an atom-style row.
func TestAtomSnapshotFreshness_ReturnsLatestNonNull(t *testing.T) {
	dbPath, raw := freshDBPath(t)

	older := "2026-06-10T13:00:00-04:00"
	newer := "2026-06-10T15:30:00-04:00"
	insertFilingWithSubmittedAt(t, raw, "0002-26-002", "0000000002", "Older Co.", &older)
	insertFilingWithSubmittedAt(t, raw, "0003-26-003", "0000000003", "Newer Co.", &newer)

	_ = raw.Close()
	s := openStore(t, dbPath)

	ts, err := s.AtomSnapshotFreshness(context.Background())
	if err != nil {
		t.Fatalf("AtomSnapshotFreshness: %v", err)
	}
	if ts == nil || *ts != newer {
		t.Errorf("freshness = %v, want %q", ts, newer)
	}
}

// TestAtomSnapshotFreshness_ReturnsNilWhenAllNull is the fresh-install case
// where every filing came in via the daily-index path with NULL submitted_at.
// The seeded fixture row is one such case on its own.
func TestAtomSnapshotFreshness_ReturnsNilWhenAllNull(t *testing.T) {
	dbPath, raw := freshDBPath(t)
	_ = raw.Close()
	s := openStore(t, dbPath)

	ts, err := s.AtomSnapshotFreshness(context.Background())
	if err != nil {
		t.Fatalf("AtomSnapshotFreshness: %v", err)
	}
	if ts != nil {
		t.Errorf("freshness = %q, want nil (no atom-ingested rows)", *ts)
	}
}
