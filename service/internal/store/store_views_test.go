package store_test

import (
	"context"
	"testing"
	"time"
)

func TestPageViewSummary(t *testing.T) {
	dbPath, raw := freshDBPath(t)
	now := time.Now().UTC()
	ins := func(path, kind, hash string, ago time.Duration) {
		radarExec(t, raw,
			`INSERT INTO page_views (path, referrer_host, client_kind, visitor_day_hash, viewed_at)
			 VALUES (?, '', ?, ?, ?)`,
			path, kind, hash, now.Add(-ago).Format(time.RFC3339),
		)
	}
	// Today: visitor A views a company page then /radar; visitor B views the landing.
	ins("/companies/0000320193", "human", "A", 1*time.Minute)
	ins("/radar", "human", "A", 2*time.Minute)
	ins("/", "human", "B", 3*time.Minute)
	// 10 days ago: visitor C on the landing (in the 30d window, not 24h/7d).
	ins("/", "human", "C", 10*24*time.Hour)
	// Non-human traffic today.
	ins("/radar", "automated", "", 1*time.Minute)
	ins("/", "crawler", "", 1*time.Minute)
	radarExec(t, raw, `INSERT INTO cik_tickers (cik, ticker, company_name, updated_at)
		VALUES ('0000320193', 'AAPL', 'APPLE INC', 't')`)
	_ = raw.Close()

	s := openStore(t, dbPath)
	st, err := s.PageViewSummary(context.Background())
	if err != nil {
		t.Fatalf("PageViewSummary: %v", err)
	}

	if st.HumanViews24h != 3 || st.HumanViews7d != 3 || st.HumanViews30d != 4 {
		t.Errorf("human views = %d/%d/%d, want 3/3/4", st.HumanViews24h, st.HumanViews7d, st.HumanViews30d)
	}
	if st.UniquesToday != 2 { // A and B
		t.Errorf("uniques today = %d, want 2", st.UniquesToday)
	}
	if st.Uniques30d != 3 { // A, B, C
		t.Errorf("uniques 30d = %d, want 3", st.Uniques30d)
	}
	if st.Automated30d != 1 || st.Crawler30d != 1 {
		t.Errorf("automated/crawler = %d/%d, want 1/1", st.Automated30d, st.Crawler30d)
	}
	// Top path: "/" (B today + C 10d ago) = 2, ahead of the single-view pages.
	if len(st.TopPaths) == 0 || st.TopPaths[0].Path != "/" || st.TopPaths[0].Count != 2 {
		t.Errorf("top paths = %+v, want '/' with 2 leading", st.TopPaths)
	}
	// Top company: the Apple page, resolved from cik_tickers.
	if len(st.TopCompanies) != 1 {
		t.Fatalf("top companies = %d, want 1", len(st.TopCompanies))
	}
	c := st.TopCompanies[0]
	if c.CIK != "0000320193" || c.Ticker != "AAPL" || c.CompanyName != "APPLE INC" || c.Count != 1 {
		t.Errorf("top company = %+v, want AAPL/APPLE INC/1", c)
	}
}
