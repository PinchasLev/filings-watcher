package server_test

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/PinchasLev/filings-watcher/service/internal/server"
	"github.com/PinchasLev/filings-watcher/service/internal/store"
)

func TestTrafficPageRenders(t *testing.T) {
	fake := &fakeStore{
		pageViewStats: store.PageViewStats{
			HumanViews24h: 42, HumanViews7d: 180, HumanViews30d: 620,
			UniquesToday: 31, Uniques30d: 210, Automated30d: 12, Crawler30d: 88,
			TopPaths: []store.PathCount{{Path: "/radar", Count: 120}, {Path: "/", Count: 90}},
			TopCompanies: []store.CompanyViewCount{
				{CIK: "0000320193", CompanyName: "APPLE INC", Ticker: "AAPL", Count: 44},
			},
		},
	}
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/ops/traffic", nil)
	server.New(fake).ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rec.Code)
	}
	body := rec.Body.String()
	for _, want := range []string{
		"Filings Radar", "traffic",
		"Human page views", "42",
		"Unique visitors", "31",
		"Most-viewed pages", "/radar", "120",
		"Most-viewed companies", "AAPL", "APPLE INC",
		"/companies/0000320193",
		"12", // automated (API-appetite) count
	} {
		if !strings.Contains(body, want) {
			t.Errorf("expected traffic body to contain %q", want)
		}
	}
}

// TestPageViewMiddlewareLogsTrackedPage confirms the wrapping middleware records a
// view for a tracked page and skips an untracked one. Logging is async (a goroutine
// off the request path), so we poll briefly for the counter.
func TestPageViewMiddlewareLogsTrackedPage(t *testing.T) {
	fake := &fakeStore{}
	h := server.New(fake)

	// A tracked page with a browser UA is logged.
	req := httptest.NewRequest(http.MethodGet, "/radar", nil)
	req.Header.Set("User-Agent", "Mozilla/5.0 Chrome/120")
	h.ServeHTTP(httptest.NewRecorder(), req)
	if !eventually(func() bool { return fake.loggedViews.Load() == 1 }) {
		t.Fatalf("tracked page view not logged (loggedViews=%d)", fake.loggedViews.Load())
	}

	// /health is not a tracked page — no additional view.
	h.ServeHTTP(httptest.NewRecorder(), httptest.NewRequest(http.MethodGet, "/health", nil))
	if eventually(func() bool { return fake.loggedViews.Load() > 1 }) {
		t.Errorf("untracked page should not be logged (loggedViews=%d)", fake.loggedViews.Load())
	}
}

func eventually(cond func() bool) bool {
	for i := 0; i < 50; i++ {
		if cond() {
			return true
		}
		time.Sleep(10 * time.Millisecond)
	}
	return cond()
}
