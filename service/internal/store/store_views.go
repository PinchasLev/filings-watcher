package store

import (
	"context"
	"fmt"
	"time"
)

// PathCount is one page and its human view total.
type PathCount struct {
	Path  string
	Count int
}

// CompanyViewCount is one company's human view total, resolved to a name/ticker.
type CompanyViewCount struct {
	CIK         string
	CompanyName string
	Ticker      string
	Count       int
}

// PageViewStats is the engagement summary shown on /ops. Human page views over a few
// trailing windows; unique visitors (distinct day-rotating hashes, so per-day, not
// cross-day) today and over 30 days; the automated and crawler volumes filtered out
// (automated = would-be API/automation demand); and the most-viewed pages and
// companies. No cross-day visitor identity is stored, so there is no returning-visitor
// count by design.
type PageViewStats struct {
	HumanViews24h int
	HumanViews7d  int
	HumanViews30d int
	UniquesToday  int
	Uniques30d    int
	Automated30d  int
	Crawler30d    int
	TopPaths      []PathCount
	TopCompanies  []CompanyViewCount
}

// LogPageView records one page view. Best-effort: the caller runs it off the request
// path and ignores the error, so analytics never affects a page render.
func (s *store) LogPageView(
	ctx context.Context, path, referrerHost, clientKind, visitorHash, viewedAt string,
) error {
	_, err := s.db.ExecContext(
		ctx,
		`INSERT INTO page_views (path, referrer_host, client_kind, visitor_day_hash, viewed_at)
		 VALUES (?, ?, ?, ?, ?)`,
		path, referrerHost, clientKind, visitorHash, viewedAt,
	)
	if err != nil {
		return fmt.Errorf("log page view: %w", err)
	}
	return nil
}

// PageViewSummary aggregates the page-view log for /ops. Window cutoffs are computed
// in the application (RFC3339 UTC) and compared as strings, so the query needs no
// database-specific date functions and stays portable.
func (s *store) PageViewSummary(ctx context.Context) (PageViewStats, error) {
	now := time.Now().UTC()
	cutoff := func(d time.Duration) string { return now.Add(-d).Format(time.RFC3339) }
	startOfToday := now.Format("2006-01-02") + "T00:00:00Z"
	cut30 := cutoff(30 * 24 * time.Hour)

	var st PageViewStats
	scalar := func(q, since string) (int, error) {
		var n int
		err := s.db.QueryRowContext(ctx, q, since).Scan(&n)
		return n, err
	}
	const humanViews = `SELECT COUNT(*) FROM page_views WHERE client_kind='human' AND viewed_at >= ?`
	const uniques = `SELECT COUNT(DISTINCT visitor_day_hash) FROM page_views
		WHERE client_kind='human' AND visitor_day_hash <> '' AND viewed_at >= ?`
	const kindViews = `SELECT COUNT(*) FROM page_views WHERE client_kind=? AND viewed_at >= ?`

	var err error
	if st.HumanViews24h, err = scalar(humanViews, cutoff(24*time.Hour)); err != nil {
		return st, fmt.Errorf("human views 24h: %w", err)
	}
	if st.HumanViews7d, err = scalar(humanViews, cutoff(7*24*time.Hour)); err != nil {
		return st, fmt.Errorf("human views 7d: %w", err)
	}
	if st.HumanViews30d, err = scalar(humanViews, cut30); err != nil {
		return st, fmt.Errorf("human views 30d: %w", err)
	}
	if st.UniquesToday, err = scalar(uniques, startOfToday); err != nil {
		return st, fmt.Errorf("uniques today: %w", err)
	}
	if st.Uniques30d, err = scalar(uniques, cut30); err != nil {
		return st, fmt.Errorf("uniques 30d: %w", err)
	}
	if err = s.db.QueryRowContext(ctx, kindViews, "automated", cut30).Scan(&st.Automated30d); err != nil {
		return st, fmt.Errorf("automated 30d: %w", err)
	}
	if err = s.db.QueryRowContext(ctx, kindViews, "crawler", cut30).Scan(&st.Crawler30d); err != nil {
		return st, fmt.Errorf("crawler 30d: %w", err)
	}

	if st.TopPaths, err = s.topPaths(ctx, cut30); err != nil {
		return st, err
	}
	if st.TopCompanies, err = s.topCompanies(ctx, cut30); err != nil {
		return st, err
	}
	return st, nil
}

func (s *store) topPaths(ctx context.Context, since string) ([]PathCount, error) {
	rows, err := s.db.QueryContext(
		ctx,
		`SELECT path, COUNT(*) AS c FROM page_views
		  WHERE client_kind='human' AND viewed_at >= ?
		  GROUP BY path ORDER BY c DESC, path LIMIT 10`,
		since,
	)
	if err != nil {
		return nil, fmt.Errorf("top paths: %w", err)
	}
	defer func() { _ = rows.Close() }()
	var out []PathCount
	for rows.Next() {
		var pc PathCount
		if err := rows.Scan(&pc.Path, &pc.Count); err != nil {
			return nil, fmt.Errorf("scan top path: %w", err)
		}
		out = append(out, pc)
	}
	return out, rows.Err()
}

// topCompanies ranks the most-viewed company pages, resolving each CIK (parsed from
// the "/companies/{cik}" path) to a name/ticker via the cik_tickers mirror. This is
// the demand-per-name signal: which companies visitors actually look at.
func (s *store) topCompanies(ctx context.Context, since string) ([]CompanyViewCount, error) {
	rows, err := s.db.QueryContext(
		ctx,
		`SELECT substr(pv.path, 12) AS cik,
		        COALESCE(ct.company_name, ''), COALESCE(ct.ticker, ''),
		        COUNT(*) AS c
		   FROM page_views pv
		   LEFT JOIN cik_tickers ct ON ct.cik = substr(pv.path, 12)
		  WHERE pv.client_kind='human' AND pv.viewed_at >= ? AND pv.path LIKE '/companies/%'
		  GROUP BY cik ORDER BY c DESC, cik LIMIT 10`,
		since,
	)
	if err != nil {
		return nil, fmt.Errorf("top companies: %w", err)
	}
	defer func() { _ = rows.Close() }()
	var out []CompanyViewCount
	for rows.Next() {
		var c CompanyViewCount
		if err := rows.Scan(&c.CIK, &c.CompanyName, &c.Ticker, &c.Count); err != nil {
			return nil, fmt.Errorf("scan top company: %w", err)
		}
		out = append(out, c)
	}
	return out, rows.Err()
}
