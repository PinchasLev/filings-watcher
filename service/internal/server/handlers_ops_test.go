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

// TestHandleOpsRendersAllThreePanels checks the happy path with all three
// panels having data: trailing-30-day total, trailing-24h total, and a
// non-nil freshness timestamp.
func TestHandleOpsRendersAllThreePanels(t *testing.T) {
	freshness := time.Now().Add(-5 * time.Minute).UTC().Format(time.RFC3339)
	fake := &fakeStore{
		trailingSpendByHours: map[int]store.SpendSnapshot{
			24 * 30: {TotalUSD: 43.21, CallCount: 1234},
			24:      {TotalUSD: 1.50, CallCount: 42},
		},
		hourlyBucketsResult: bucketsAllZero(24),
		freshnessResult:     &freshness,
	}

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/ops/", nil)
	server.New(fake).ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rec.Code)
	}
	body := rec.Body.String()

	for _, want := range []string{
		"Trailing 30 days spend",
		"$43.21",
		"1234 LLM call",
		"Trailing 24 hours spend",
		"$1.50",
		"42 LLM call",
		"Atom-ingest freshness",
		"min ago",
		"Hourly spend, last 24 hours",
	} {
		if !strings.Contains(body, want) {
			t.Errorf("response missing %q", want)
		}
	}
}

// TestHandleOpsStripsPublicChrome ensures the operator dashboard does NOT
// inherit the public site's header (ticker search form, Latest/Live nav).
// Those bled in from layout.html.tmpl in the first cut and were a UX bug.
func TestHandleOpsStripsPublicChrome(t *testing.T) {
	fake := &fakeStore{
		trailingSpendByHours: map[int]store.SpendSnapshot{
			24 * 30: {},
			24:      {},
		},
		hourlyBucketsResult: bucketsAllZero(24),
	}

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/ops/", nil)
	server.New(fake).ServeHTTP(rec, req)

	body := rec.Body.String()
	for _, banned := range []string{
		`name="ticker"`, // public search form
		"Search by ticker",
		`href="/live"`, // Latest/Live nav
		"page-nav",     // the nav CSS class
	} {
		if strings.Contains(body, banned) {
			t.Errorf("response should not contain %q (public chrome leaked into /ops)", banned)
		}
	}
	// A back link to the public site is fine — it's the operator's
	// "let me peek at what users see" affordance.
	if !strings.Contains(body, `href="/"`) {
		t.Errorf("response should contain a back-to-site link")
	}
}

// TestHandleOpsRendersOneRectPerBucket confirms the SVG chart renders 24
// <rect> elements — one per trailing-hour bucket — even when every bucket
// has zero spend. Each rect should carry the floor height so the axis
// still reads as 24 evenly-spaced bars.
func TestHandleOpsRendersOneRectPerBucket(t *testing.T) {
	fake := &fakeStore{
		trailingSpendByHours: map[int]store.SpendSnapshot{
			24 * 30: {},
			24:      {},
		},
		hourlyBucketsResult: bucketsAllZero(24),
	}

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/ops/", nil)
	server.New(fake).ServeHTTP(rec, req)

	body := rec.Body.String()
	if got := strings.Count(body, "<rect "); got != 24 {
		t.Errorf("<rect> count = %d, want 24", got)
	}
}

// TestHandleOpsHandlesNoFreshnessData verifies the "no data" branch.
func TestHandleOpsHandlesNoFreshnessData(t *testing.T) {
	fake := &fakeStore{
		trailingSpendByHours: map[int]store.SpendSnapshot{
			24 * 30: {},
			24:      {},
		},
		hourlyBucketsResult: bucketsAllZero(24),
		freshnessResult:     nil,
	}

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/ops/", nil)
	server.New(fake).ServeHTTP(rec, req)

	body := rec.Body.String()
	if !strings.Contains(body, "no data") {
		t.Errorf("expected freshness panel to render 'no data' when timestamp is nil")
	}
}

// TestHandleOpsCallsStoreWithBothWindows checks that both 30-day and 24h
// windows were queried.
func TestHandleOpsCallsStoreWithBothWindows(t *testing.T) {
	fake := &fakeStore{
		trailingSpendByHours: map[int]store.SpendSnapshot{
			24 * 30: {},
			24:      {},
		},
		hourlyBucketsResult: bucketsAllZero(24),
	}

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/ops/", nil)
	server.New(fake).ServeHTTP(rec, req)

	gotHours := map[int]bool{}
	for _, h := range fake.trailingSpendCalledWith {
		gotHours[h] = true
	}
	if !gotHours[24] || !gotHours[24*30] {
		t.Errorf("expected TrailingHoursSpend called with both 24 and 720, got %v",
			fake.trailingSpendCalledWith)
	}
	if len(fake.hourlyBucketsCalledWith) != 1 || fake.hourlyBucketsCalledWith[0] != 24 {
		t.Errorf("expected HourlySpendBuckets called once with 24, got %v",
			fake.hourlyBucketsCalledWith)
	}
}

// bucketsAllZero produces a slice of n hourly buckets with totals=0,
// matching the store's zero-padded output shape.
func bucketsAllZero(n int) []store.HourlyBucket {
	out := make([]store.HourlyBucket, n)
	base := time.Now().UTC().Truncate(time.Hour).Add(-time.Duration(n-1) * time.Hour)
	for i := 0; i < n; i++ {
		out[i] = store.HourlyBucket{
			HourStart: base.Add(time.Duration(i) * time.Hour).Format(time.RFC3339),
			TotalUSD:  0,
		}
	}
	return out
}
