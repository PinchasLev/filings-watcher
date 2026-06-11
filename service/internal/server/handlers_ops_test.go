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

// TestHandleOpsRendersAllPanels checks the happy path with both spend
// totals, both charts, and a non-nil freshness timestamp populated.
func TestHandleOpsRendersAllPanels(t *testing.T) {
	freshness := time.Now().Add(-5 * time.Minute).UTC().Format(time.RFC3339)
	fake := &fakeStore{
		trailingSpendByHours: map[int]store.SpendSnapshot{
			24 * 30: {TotalUSD: 43.21, CallCount: 1234},
			24:      {TotalUSD: 1.50, CallCount: 42},
		},
		hourlyBucketsResult: hourlyZeros(24),
		dailyBucketsResult:  dailyZeros(30),
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
		"Daily spend, last 30 days",
		"30 days ago",
	} {
		if !strings.Contains(body, want) {
			t.Errorf("response missing %q", want)
		}
	}
}

// TestHandleOpsStripsPublicChrome ensures the operator dashboard does not
// inherit the public site's header (ticker search form, Latest/Live nav).
func TestHandleOpsStripsPublicChrome(t *testing.T) {
	fake := &fakeStore{
		trailingSpendByHours: map[int]store.SpendSnapshot{24 * 30: {}, 24: {}},
		hourlyBucketsResult:  hourlyZeros(24),
		dailyBucketsResult:   dailyZeros(30),
	}

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/ops/", nil)
	server.New(fake).ServeHTTP(rec, req)

	body := rec.Body.String()
	for _, banned := range []string{
		`name="ticker"`,
		"Search by ticker",
		`href="/live"`,
		"page-nav",
	} {
		if strings.Contains(body, banned) {
			t.Errorf("response should not contain %q (public chrome leaked)", banned)
		}
	}
	if !strings.Contains(body, `href="/"`) {
		t.Errorf("response should contain a back-to-site link")
	}
}

// TestHandleOpsRenders24RectsForHourlyChartPlus30ForDaily confirms each
// chart panel renders exactly one <rect class="bar"> per source bucket.
func TestHandleOpsRenders24RectsForHourlyChartPlus30ForDaily(t *testing.T) {
	fake := &fakeStore{
		trailingSpendByHours: map[int]store.SpendSnapshot{24 * 30: {}, 24: {}},
		hourlyBucketsResult:  hourlyZeros(24),
		dailyBucketsResult:   dailyZeros(30),
	}

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/ops/", nil)
	server.New(fake).ServeHTTP(rec, req)

	body := rec.Body.String()
	if got := strings.Count(body, `<rect class="bar"`); got != 24+30 {
		t.Errorf("<rect class=\"bar\"> count = %d, want %d (24 hourly + 30 daily)", got, 24+30)
	}
}

// TestHandleOpsRendersYAxisLabels confirms that when there is real
// non-zero data, the Y-axis renders peak / mid / zero tick labels in the
// SVG. Each chart should have three text labels.
func TestHandleOpsRendersYAxisLabels(t *testing.T) {
	now := time.Now().UTC().Truncate(time.Hour)
	hourly := make([]store.HourlyBucket, 24)
	for i := 0; i < 24; i++ {
		hourly[i] = store.HourlyBucket{
			HourStart: now.Add(time.Duration(i-23) * time.Hour).Format(time.RFC3339),
		}
	}
	// One spike of $0.10 produces a non-zero peak.
	hourly[12].TotalUSD = 0.10

	dayStart := time.Now().UTC().Truncate(24 * time.Hour)
	daily := make([]store.DailyBucket, 30)
	for i := 0; i < 30; i++ {
		daily[i] = store.DailyBucket{
			DayStart: dayStart.Add(time.Duration(i-29) * 24 * time.Hour).Format(time.RFC3339),
		}
	}
	daily[15].TotalUSD = 5.50

	fake := &fakeStore{
		trailingSpendByHours: map[int]store.SpendSnapshot{24 * 30: {}, 24: {}},
		hourlyBucketsResult:  hourly,
		dailyBucketsResult:   daily,
	}

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/ops/", nil)
	server.New(fake).ServeHTTP(rec, req)

	body := rec.Body.String()
	// Peak labels — the values we seeded.
	for _, want := range []string{
		"$0.1000",
		"$0.0500", // mid (peak/2) for hourly
		"$5.5000",
		"$2.7500", // mid (peak/2) for daily
	} {
		if !strings.Contains(body, want) {
			t.Errorf("response missing Y-axis label %q", want)
		}
	}
	// Gridlines: each chart has 3 ticks, so 6 total <line class="gridline">.
	if got := strings.Count(body, `class="gridline"`); got != 6 {
		t.Errorf("gridline count = %d, want 6 (3 per chart)", got)
	}
}

// TestHandleOpsHandlesNoFreshnessData verifies the "no data" branch.
func TestHandleOpsHandlesNoFreshnessData(t *testing.T) {
	fake := &fakeStore{
		trailingSpendByHours: map[int]store.SpendSnapshot{24 * 30: {}, 24: {}},
		hourlyBucketsResult:  hourlyZeros(24),
		dailyBucketsResult:   dailyZeros(30),
		freshnessResult:      nil,
	}

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/ops/", nil)
	server.New(fake).ServeHTTP(rec, req)

	body := rec.Body.String()
	if !strings.Contains(body, "no data") {
		t.Errorf("expected freshness panel to render 'no data' when timestamp is nil")
	}
}

// TestHandleOpsCallsStoreWithRightWindows checks all three of the rolling
// queries were dispatched with the expected windows.
func TestHandleOpsCallsStoreWithRightWindows(t *testing.T) {
	fake := &fakeStore{
		trailingSpendByHours: map[int]store.SpendSnapshot{24 * 30: {}, 24: {}},
		hourlyBucketsResult:  hourlyZeros(24),
		dailyBucketsResult:   dailyZeros(30),
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
	if len(fake.dailyBucketsCalledWith) != 1 || fake.dailyBucketsCalledWith[0] != 30 {
		t.Errorf("expected DailySpendBuckets called once with 30, got %v",
			fake.dailyBucketsCalledWith)
	}
}

func hourlyZeros(n int) []store.HourlyBucket {
	out := make([]store.HourlyBucket, n)
	base := time.Now().UTC().Truncate(time.Hour).Add(-time.Duration(n-1) * time.Hour)
	for i := 0; i < n; i++ {
		out[i] = store.HourlyBucket{
			HourStart: base.Add(time.Duration(i) * time.Hour).Format(time.RFC3339),
		}
	}
	return out
}

func dailyZeros(n int) []store.DailyBucket {
	out := make([]store.DailyBucket, n)
	base := time.Now().UTC().Truncate(24 * time.Hour).Add(-time.Duration(n-1) * 24 * time.Hour)
	for i := 0; i < n; i++ {
		out[i] = store.DailyBucket{
			DayStart: base.Add(time.Duration(i) * 24 * time.Hour).Format(time.RFC3339),
		}
	}
	return out
}
