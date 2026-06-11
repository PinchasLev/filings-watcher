// Read-only queries that back the operator dashboard at /ops/. These
// aggregate the existing transactional tables; no schema additions and
// nothing the public-facing pages read. The dashboard is the operator's
// at-a-glance view of cost trajectory and ingest freshness — the first
// surface that makes the observability foundation actually usable.

package store

import (
	"context"
	"database/sql"
	"fmt"
)

// TodaySpend returns today's Anthropic spend and call count, defined on the
// UTC calendar day. Matches the orchestrator's pre-tick cap check so the
// dashboard and the gate agree on what "today" means.
func (s *store) TodaySpend(ctx context.Context) (SpendSnapshot, error) {
	const q = `
		SELECT COALESCE(SUM(estimated_cost_usd), 0.0), COUNT(*)
		  FROM llm_calls
		 WHERE substr(emitted_at, 1, 10) = strftime('%Y-%m-%d', 'now')
	`
	var snap SpendSnapshot
	if err := s.db.QueryRowContext(ctx, q).Scan(&snap.TotalUSD, &snap.CallCount); err != nil {
		return SpendSnapshot{}, fmt.Errorf("today spend: %w", err)
	}
	return snap, nil
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
