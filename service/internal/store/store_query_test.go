package store_test

import (
	"context"
	"testing"
)

func TestRunReadOnlyQuery(t *testing.T) {
	dbPath, raw := freshDBPath(t)
	radarExec(t, raw,
		`INSERT INTO page_views (path, referrer_host, client_kind, visitor_day_hash, viewed_at)
		 VALUES ('/radar', '', 'human', 'A', '2026-07-27T10:00:00Z')`)
	_ = raw.Close()
	s := openStore(t, dbPath)
	ctx := context.Background()

	cols, rows, truncated, err := s.RunReadOnlyQuery(ctx,
		"SELECT path, client_kind FROM page_views ORDER BY viewed_at DESC")
	if err != nil {
		t.Fatalf("valid SELECT: %v", err)
	}
	if len(cols) != 2 || cols[0] != "path" || cols[1] != "client_kind" {
		t.Errorf("cols = %v, want [path client_kind]", cols)
	}
	if len(rows) != 1 || rows[0][0] != "/radar" || rows[0][1] != "human" {
		t.Errorf("rows = %v, want [[/radar human]]", rows)
	}
	if truncated {
		t.Errorf("truncated = true, want false")
	}
}

func TestRunReadOnlyQueryRejectsWrites(t *testing.T) {
	dbPath, raw := freshDBPath(t)
	_ = raw.Close()
	s := openStore(t, dbPath)
	ctx := context.Background()

	for _, q := range []string{
		"DELETE FROM page_views",
		"UPDATE page_views SET path='x'",
		"DROP TABLE page_views",
		"SELECT 1; DROP TABLE page_views",             // stacked statement
		"WITH x AS (SELECT 1) DELETE FROM page_views", // write behind a CTE
		"",
	} {
		if _, _, _, err := s.RunReadOnlyQuery(ctx, q); err == nil {
			t.Errorf("expected %q to be rejected, but it ran", q)
		}
	}
}

func TestRunReadOnlyQueryCaps(t *testing.T) {
	dbPath, raw := freshDBPath(t)
	// A recursive CTE generating 600 rows exercises the 500-row cap.
	_ = raw.Close()
	s := openStore(t, dbPath)
	q := `WITH RECURSIVE seq(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM seq WHERE n < 600)
	      SELECT n FROM seq`
	_, rows, truncated, err := s.RunReadOnlyQuery(context.Background(), q)
	if err != nil {
		t.Fatalf("recursive SELECT: %v", err)
	}
	if len(rows) != 500 || !truncated {
		t.Errorf("rows=%d truncated=%v, want 500 rows + truncated", len(rows), truncated)
	}
}
