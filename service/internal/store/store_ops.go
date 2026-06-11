// Read-only queries that back the operator dashboard at /ops/. These
// aggregate the existing transactional tables; no schema additions and
// nothing the public-facing pages read. The dashboard is the operator's
// at-a-glance view of cost trajectory and ingest freshness — the first
// surface that makes the observability foundation actually usable.
//
// Spend windows are rolling, not calendar-aligned. Calendar boundaries
// are arbitrary reset points ("today's spend" is a full day at 11:59pm
// UTC, near-zero at 12:01am) and don't match how an operator thinks
// about recent usage or pre-paid balance depletion. The orchestrator's
// safety gate keeps the calendar-day framing because it needs a hard
// reset point; the dashboard's separate question allows a separate
// answer.

package store

import (
	"context"
	"database/sql"
	"fmt"
	"time"
)

// TrailingHoursSpend returns the cost and call-count totals over the
// last `hours` hours, computed against `emitted_at` (ISO 8601 UTC).
// The same query backs both the trailing-30-days budget panel
// (hours=720) and the trailing-24h behavior panel (hours=24).
func (s *store) TrailingHoursSpend(ctx context.Context, hours int) (SpendSnapshot, error) {
	cutoff := time.Now().UTC().Add(-time.Duration(hours) * time.Hour).Format(time.RFC3339Nano)
	const q = `
		SELECT COALESCE(SUM(estimated_cost_usd), 0.0), COUNT(*)
		  FROM llm_calls
		 WHERE emitted_at >= ?
	`
	var snap SpendSnapshot
	if err := s.db.QueryRowContext(ctx, q, cutoff).Scan(&snap.TotalUSD, &snap.CallCount); err != nil {
		return SpendSnapshot{}, fmt.Errorf("trailing hours spend: %w", err)
	}
	return snap, nil
}

// HourlySpendBuckets returns one bucket per trailing hour, oldest first,
// zero-padded so empty hours still appear. The chart relies on the
// zero-padding to keep its x-axis uniform — without it, a sparse run
// would render with arbitrarily-narrow bars where hours had no calls.
//
// The bucket hour is the floor-of-hour UTC of when each call was emitted.
// The right-most bucket covers the current (incomplete) hour, so as time
// passes within an hour, that bar grows.
func (s *store) HourlySpendBuckets(ctx context.Context, hours int) ([]HourlyBucket, error) {
	now := time.Now().UTC().Truncate(time.Hour)
	start := now.Add(-time.Duration(hours-1) * time.Hour)

	const q = `
		SELECT strftime('%Y-%m-%dT%H:00:00Z', emitted_at) AS bucket,
		       COALESCE(SUM(estimated_cost_usd), 0.0)
		  FROM llm_calls
		 WHERE emitted_at >= ?
		 GROUP BY bucket
	`
	rows, err := s.db.QueryContext(ctx, q, start.Format(time.RFC3339Nano))
	if err != nil {
		return nil, fmt.Errorf("hourly spend buckets: %w", err)
	}
	defer rows.Close()

	seen := make(map[string]float64, hours)
	for rows.Next() {
		var bucket string
		var total float64
		if err := rows.Scan(&bucket, &total); err != nil {
			return nil, fmt.Errorf("scan hourly bucket: %w", err)
		}
		seen[bucket] = total
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("iterate hourly buckets: %w", err)
	}

	out := make([]HourlyBucket, 0, hours)
	for i := 0; i < hours; i++ {
		hour := start.Add(time.Duration(i) * time.Hour).Format("2006-01-02T15:04:05Z")
		out = append(out, HourlyBucket{HourStart: hour, TotalUSD: seen[hour]})
	}
	return out, nil
}

// DailySpendBuckets returns one bucket per trailing day, oldest first,
// zero-padded so empty days still appear. The bucket day is the floor-of-day
// UTC of when each call was emitted. The right-most bucket covers the
// current (incomplete) day, so as the day progresses, that bar grows.
//
// Symmetric to HourlySpendBuckets but at the trailing-30-day horizon. The
// chart this backs answers the budget-side question: "is my burn rate
// steady, accelerating, or driven by a couple of bad days?"
func (s *store) DailySpendBuckets(ctx context.Context, days int) ([]DailyBucket, error) {
	now := time.Now().UTC().Truncate(24 * time.Hour)
	start := now.Add(-time.Duration(days-1) * 24 * time.Hour)

	const q = `
		SELECT strftime('%Y-%m-%dT00:00:00Z', emitted_at) AS bucket,
		       COALESCE(SUM(estimated_cost_usd), 0.0)
		  FROM llm_calls
		 WHERE emitted_at >= ?
		 GROUP BY bucket
	`
	rows, err := s.db.QueryContext(ctx, q, start.Format(time.RFC3339Nano))
	if err != nil {
		return nil, fmt.Errorf("daily spend buckets: %w", err)
	}
	defer rows.Close()

	seen := make(map[string]float64, days)
	for rows.Next() {
		var bucket string
		var total float64
		if err := rows.Scan(&bucket, &total); err != nil {
			return nil, fmt.Errorf("scan daily bucket: %w", err)
		}
		seen[bucket] = total
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("iterate daily buckets: %w", err)
	}

	out := make([]DailyBucket, 0, days)
	for i := 0; i < days; i++ {
		day := start.Add(time.Duration(i) * 24 * time.Hour).Format("2006-01-02T15:04:05Z")
		out = append(out, DailyBucket{DayStart: day, TotalUSD: seen[day]})
	}
	return out, nil
}

// SpendDataStartDate returns the UTC date of the earliest llm_calls row,
// formatted "YYYY-MM-DD". Returns "" when the table is empty. Used by
// the dashboard to caveat the 30-day chart when instrumentation started
// inside the window — otherwise days predating cost-capture would look
// like genuine zero-spend days rather than no-data days.
func (s *store) SpendDataStartDate(ctx context.Context) (string, error) {
	const q = `SELECT substr(MIN(emitted_at), 1, 10) FROM llm_calls`
	var raw sql.NullString
	if err := s.db.QueryRowContext(ctx, q).Scan(&raw); err != nil {
		return "", fmt.Errorf("spend data start date: %w", err)
	}
	if !raw.Valid {
		return "", nil
	}
	return raw.String, nil
}

// AtomSnapshotFreshness returns the most recent EDGAR-side submission
// timestamp recorded across the filings table — the high-water mark of
// what the atom ingest path has captured. The handler renders the gap
// from this to "now" so a stalled ingest pipeline is visible.
//
// Returns (nil, nil) when no filing has a submitted_at yet (e.g., a fresh
// install or pre-atom-path corpus). The handler treats nil as "no data."
func (s *store) AtomSnapshotFreshness(ctx context.Context) (*string, error) {
	const q = `
		SELECT submitted_at
		  FROM filings
		 WHERE submitted_at IS NOT NULL
		 ORDER BY datetime(submitted_at) DESC
		 LIMIT 1
	`
	var ts sql.NullString
	if err := s.db.QueryRowContext(ctx, q).Scan(&ts); err != nil {
		if err == sql.ErrNoRows {
			return nil, nil
		}
		return nil, fmt.Errorf("atom snapshot freshness: %w", err)
	}
	if !ts.Valid {
		return nil, nil
	}
	return &ts.String, nil
}
