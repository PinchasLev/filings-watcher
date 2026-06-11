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

// TestTrailingHoursSpend_RollingWindowExcludesOlderRows checks the cutoff
// math: a row 25 hours ago should not appear in the trailing-24h window.
func TestTrailingHoursSpend_RollingWindowExcludesOlderRows(t *testing.T) {
	dbPath, raw := freshDBPath(t)

	now := time.Now().UTC()
	insertLLMCall(t, raw, now.Add(-30*time.Minute).Format(time.RFC3339Nano), 0.50) // in
	insertLLMCall(t, raw, now.Add(-12*time.Hour).Format(time.RFC3339Nano), 1.00)   // in
	insertLLMCall(t, raw, now.Add(-25*time.Hour).Format(time.RFC3339Nano), 99.00)  // out

	_ = raw.Close()
	s := openStore(t, dbPath)

	snap, err := s.TrailingHoursSpend(context.Background(), 24)
	if err != nil {
		t.Fatalf("TrailingHoursSpend(24): %v", err)
	}
	if snap.CallCount != 2 {
		t.Errorf("CallCount = %d, want 2 (>24h row excluded)", snap.CallCount)
	}
	if delta := snap.TotalUSD - 1.50; delta < -0.001 || delta > 0.001 {
		t.Errorf("TotalUSD = %f, want 1.50", snap.TotalUSD)
	}
}

// TestTrailingHoursSpend_30DaysIncludesOlderRows confirms the same function
// produces a wider window when called with 720 hours, used by the budget panel.
func TestTrailingHoursSpend_30DaysIncludesOlderRows(t *testing.T) {
	dbPath, raw := freshDBPath(t)

	now := time.Now().UTC()
	insertLLMCall(t, raw, now.Add(-30*time.Minute).Format(time.RFC3339Nano), 0.50)  // in 24h, in 30d
	insertLLMCall(t, raw, now.Add(-10*24*time.Hour).Format(time.RFC3339Nano), 2.00) // out 24h, in 30d
	insertLLMCall(t, raw, now.Add(-40*24*time.Hour).Format(time.RFC3339Nano), 9.00) // out both

	_ = raw.Close()
	s := openStore(t, dbPath)

	snap, err := s.TrailingHoursSpend(context.Background(), 24*30)
	if err != nil {
		t.Fatalf("TrailingHoursSpend(720): %v", err)
	}
	if snap.CallCount != 2 {
		t.Errorf("CallCount = %d, want 2 (40d-old row excluded)", snap.CallCount)
	}
	if delta := snap.TotalUSD - 2.50; delta < -0.001 || delta > 0.001 {
		t.Errorf("TotalUSD = %f, want 2.50", snap.TotalUSD)
	}
}

// TestTrailingHoursSpend_ReturnsZeroWhenEmpty confirms the COALESCE handles
// the fresh-DB case.
func TestTrailingHoursSpend_ReturnsZeroWhenEmpty(t *testing.T) {
	dbPath, raw := freshDBPath(t)
	_ = raw.Close()
	s := openStore(t, dbPath)

	snap, err := s.TrailingHoursSpend(context.Background(), 24)
	if err != nil {
		t.Fatalf("TrailingHoursSpend: %v", err)
	}
	if snap.CallCount != 0 || snap.TotalUSD != 0.0 {
		t.Errorf("empty result = %+v, want zero values", snap)
	}
}

// TestHourlySpendBuckets_ZeroPadsEmptyHours is the central guarantee for the
// chart: hours with no llm_calls still produce a bucket with TotalUSD=0,
// so the SVG x-axis stays uniform.
func TestHourlySpendBuckets_ZeroPadsEmptyHours(t *testing.T) {
	dbPath, raw := freshDBPath(t)

	now := time.Now().UTC()
	// Seed exactly one row, 3 hours ago.
	insertLLMCall(t, raw, now.Add(-3*time.Hour).Format(time.RFC3339Nano), 1.00)

	_ = raw.Close()
	s := openStore(t, dbPath)

	buckets, err := s.HourlySpendBuckets(context.Background(), 24)
	if err != nil {
		t.Fatalf("HourlySpendBuckets: %v", err)
	}
	if len(buckets) != 24 {
		t.Fatalf("len = %d, want 24 (zero-padded)", len(buckets))
	}
	// Exactly one bucket should be non-zero.
	nonZero := 0
	for _, b := range buckets {
		if b.TotalUSD > 0 {
			nonZero++
		}
	}
	if nonZero != 1 {
		t.Errorf("non-zero buckets = %d, want 1", nonZero)
	}
}

// TestHourlySpendBuckets_OrderedOldestFirst — the chart expects bars from
// 24h-ago on the left to "now" on the right.
func TestHourlySpendBuckets_OrderedOldestFirst(t *testing.T) {
	dbPath, raw := freshDBPath(t)
	_ = raw.Close()
	s := openStore(t, dbPath)

	buckets, err := s.HourlySpendBuckets(context.Background(), 24)
	if err != nil {
		t.Fatalf("HourlySpendBuckets: %v", err)
	}
	if len(buckets) != 24 {
		t.Fatalf("len = %d, want 24", len(buckets))
	}
	// Verify monotonically-increasing HourStart by parsing.
	prev, _ := time.Parse(time.RFC3339, buckets[0].HourStart)
	for i := 1; i < len(buckets); i++ {
		cur, err := time.Parse(time.RFC3339, buckets[i].HourStart)
		if err != nil {
			t.Fatalf("parse bucket[%d] = %q: %v", i, buckets[i].HourStart, err)
		}
		if !cur.After(prev) {
			t.Errorf("bucket[%d] (%v) not after bucket[%d] (%v)", i, cur, i-1, prev)
		}
		prev = cur
	}
}

// TestDailySpendBuckets_ZeroPadsEmptyDays mirrors the hourly-bucket guarantee
// at a longer horizon: every day in the window appears, even if no llm_calls
// landed that day, so the chart's x-axis stays uniform.
func TestDailySpendBuckets_ZeroPadsEmptyDays(t *testing.T) {
	dbPath, raw := freshDBPath(t)

	now := time.Now().UTC()
	// Seed exactly one row, 5 days ago.
	insertLLMCall(t, raw, now.Add(-5*24*time.Hour).Format(time.RFC3339Nano), 2.50)

	_ = raw.Close()
	s := openStore(t, dbPath)

	buckets, err := s.DailySpendBuckets(context.Background(), 30)
	if err != nil {
		t.Fatalf("DailySpendBuckets: %v", err)
	}
	if len(buckets) != 30 {
		t.Fatalf("len = %d, want 30 (zero-padded)", len(buckets))
	}
	nonZero := 0
	for _, b := range buckets {
		if b.TotalUSD > 0 {
			nonZero++
		}
	}
	if nonZero != 1 {
		t.Errorf("non-zero buckets = %d, want 1", nonZero)
	}
}

// TestDailySpendBuckets_OrderedOldestFirst — the chart expects bars from
// 30d-ago on the left to today on the right.
func TestDailySpendBuckets_OrderedOldestFirst(t *testing.T) {
	dbPath, raw := freshDBPath(t)
	_ = raw.Close()
	s := openStore(t, dbPath)

	buckets, err := s.DailySpendBuckets(context.Background(), 30)
	if err != nil {
		t.Fatalf("DailySpendBuckets: %v", err)
	}
	if len(buckets) != 30 {
		t.Fatalf("len = %d, want 30", len(buckets))
	}
	prev, _ := time.Parse(time.RFC3339, buckets[0].DayStart)
	for i := 1; i < len(buckets); i++ {
		cur, err := time.Parse(time.RFC3339, buckets[i].DayStart)
		if err != nil {
			t.Fatalf("parse bucket[%d] = %q: %v", i, buckets[i].DayStart, err)
		}
		if !cur.After(prev) {
			t.Errorf("bucket[%d] (%v) not after bucket[%d] (%v)", i, cur, i-1, prev)
		}
		prev = cur
	}
}

// TestAtomSnapshotFreshness_ReturnsLatestNonNull ignores any daily-index-style
// filing (no submitted_at) and returns the explicit timestamp from an
// atom-style row.
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

// TestAtomSnapshotFreshness_ReturnsNilWhenAllNull — fresh-install case.
func TestAtomSnapshotFreshness_ReturnsNilWhenAllNull(t *testing.T) {
	dbPath, raw := freshDBPath(t)
	_ = raw.Close()
	s := openStore(t, dbPath)

	ts, err := s.AtomSnapshotFreshness(context.Background())
	if err != nil {
		t.Fatalf("AtomSnapshotFreshness: %v", err)
	}
	if ts != nil {
		t.Errorf("freshness = %q, want nil", *ts)
	}
}
