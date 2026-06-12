package server_test

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/PinchasLev/filings-watcher/service/internal/server"
	"github.com/PinchasLev/filings-watcher/service/internal/store"
)

// TestHandleLiveSinceHappyPath: valid since, store returns N, JSON shape
// is correct and includes "now".
func TestHandleLiveSinceHappyPath(t *testing.T) {
	fake := &fakeStore{countLiveSinceResult: 5}

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/api/live-since?since=2026-06-11T15:00:00Z", nil)
	server.New(fake).ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200", rec.Code)
	}
	if got := rec.Header().Get("Content-Type"); !strings.HasPrefix(got, "application/json") {
		t.Errorf("Content-Type = %q, want application/json", got)
	}
	if got := rec.Header().Get("Cache-Control"); got != "no-store" {
		t.Errorf("Cache-Control = %q, want no-store", got)
	}

	var body struct {
		NewCount int    `json:"new_count"`
		Now      string `json:"now"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("decode response: %v", err)
	}
	if body.NewCount != 5 {
		t.Errorf("new_count = %d, want 5", body.NewCount)
	}
	if body.Now == "" {
		t.Errorf("now is empty; want RFC3339 timestamp")
	}
}

// TestHandleLiveSinceMissingSince: empty ?since= → 400.
func TestHandleLiveSinceMissingSince(t *testing.T) {
	fake := &fakeStore{}
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/api/live-since", nil)
	server.New(fake).ServeHTTP(rec, req)

	if rec.Code != http.StatusBadRequest {
		t.Errorf("status = %d, want 400", rec.Code)
	}
}

// TestHandleLiveSinceInvalidSince: malformed ?since= → 400.
func TestHandleLiveSinceInvalidSince(t *testing.T) {
	fake := &fakeStore{}
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/api/live-since?since=yesterday", nil)
	server.New(fake).ServeHTTP(rec, req)

	if rec.Code != http.StatusBadRequest {
		t.Errorf("status = %d, want 400", rec.Code)
	}
}

// TestHandleLiveSincePassesRFC3339ThroughToStore confirms the parsed time
// flows correctly to CountLiveEventsSince.
func TestHandleLiveSincePassesRFC3339ThroughToStore(t *testing.T) {
	fake := &fakeStore{}
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/api/live-since?since=2026-06-11T15:00:00Z", nil)
	server.New(fake).ServeHTTP(rec, req)

	want, _ := time.Parse(time.RFC3339, "2026-06-11T15:00:00Z")
	if len(fake.countLiveSinceCalledWith) != 1 || !fake.countLiveSinceCalledWith[0].Equal(want) {
		t.Errorf("CountLiveEventsSince called with %v, want %v", fake.countLiveSinceCalledWith, want)
	}
}

// TestHandleLiveScriptServesEmbeddedFile checks the /static/live.js route
// returns the embedded JS with the right Content-Type.
func TestHandleLiveScriptServesEmbeddedFile(t *testing.T) {
	fake := &fakeStore{}
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/static/live.js", nil)
	server.New(fake).ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200", rec.Code)
	}
	if got := rec.Header().Get("Content-Type"); !strings.HasPrefix(got, "application/javascript") {
		t.Errorf("Content-Type = %q, want application/javascript", got)
	}
	body := rec.Body.String()
	for _, want := range []string{
		"live-banner",     // CSS class created in the JS
		"/api/live-since", // endpoint the script polls
		"data-since",      // attribute the script reads
		"new filing",      // banner text format string
	} {
		if !strings.Contains(body, want) {
			t.Errorf("served live.js missing %q", want)
		}
	}
}

// TestHandleLiveEmbedsScriptTag confirms the /live page renders the
// <script src="/static/live.js" data-since="..."> tag so the poller
// has a baseline timestamp.
func TestHandleLiveEmbedsScriptTag(t *testing.T) {
	fake := &fakeStore{}

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/live", nil)
	server.New(fake).ServeHTTP(rec, req)

	body := rec.Body.String()
	if !strings.Contains(body, `src="/static/live.js"`) {
		t.Errorf("/live response missing script src=/static/live.js")
	}
	if !strings.Contains(body, `data-since="`) {
		t.Errorf("/live response missing data-since attribute")
	}
}

// TestHandleLiveBannerSinceIsNewestVisibleEventTimestamp confirms the
// freshness banner is anchored to the newest visible event's
// submitted_at, not to page-render time. That's the fix for the "tab
// open for hours" UX problem where the banner accumulated a count of
// filings that would already be on the page after refresh.
func TestHandleLiveBannerSinceIsNewestVisibleEventTimestamp(t *testing.T) {
	newest := "2026-06-11T17:30:43-04:00"
	older := "2026-06-11T14:00:00-04:00"
	fake := &fakeStore{
		liveEventsResult: []store.Event{
			{
				AccessionNumber: "0001-26-001",
				IsMaterial:      true,
				SubmittedAt:     &newest, // events sorted DESC: this is at index 0
				CompanyName:     "Acme",
				FilingDate:      "2026-06-11",
			},
			{
				AccessionNumber: "0002-26-002",
				IsMaterial:      true,
				SubmittedAt:     &older,
				CompanyName:     "Older Co.",
				FilingDate:      "2026-06-11",
			},
		},
		liveEventsTotal: 2,
	}

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/live", nil)
	server.New(fake).ServeHTTP(rec, req)

	body := rec.Body.String()
	// data-since must match the newest visible event's submitted_at.
	// HTML-escaping turns the ":" inside the offset into "&#43;"? No —
	// only "&", "<", ">", "\"", "'" are escaped by html/template's
	// default contextual auto-escaping for attribute values. Colons
	// pass through verbatim.
	want := `data-since="` + newest + `"`
	if !strings.Contains(body, want) {
		t.Errorf("/live response missing %q; banner would re-fire on already-visible events", want)
	}
}

// TestHandleLiveBannerSinceFallsBackToNowOnEmptyWindow confirms the
// empty-window path. When no events render, the banner has no event to
// anchor against and falls back to "now" so it stays quiet until
// something genuinely new arrives.
func TestHandleLiveBannerSinceFallsBackToNowOnEmptyWindow(t *testing.T) {
	fake := &fakeStore{liveEventsResult: nil, liveEventsTotal: 0}

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/live", nil)
	server.New(fake).ServeHTTP(rec, req)

	body := rec.Body.String()
	if !strings.Contains(body, `data-since="`) {
		t.Errorf("/live response missing data-since attribute on empty window")
	}
	// On empty window the data-since should be a current UTC timestamp
	// (Z suffix), not a stale one. A loose check that it starts with the
	// current year is sufficient — exact-time matching is racy.
	thisYear := time.Now().UTC().Format("2006")
	if !strings.Contains(body, `data-since="`+thisYear) {
		t.Errorf("expected data-since to start with current year %q", thisYear)
	}
}

// TestHandleOpsHasMetaRefresh confirms the /ops page auto-refreshes via
// the meta tag — no JS, no CSP relaxation needed for that page.
func TestHandleOpsHasMetaRefresh(t *testing.T) {
	fake := &fakeStore{
		hourlyBucketsResult: hourlyZeros(24),
		dailyBucketsResult:  dailyZeros(30),
	}

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/ops/", nil)
	server.New(fake).ServeHTTP(rec, req)

	body := rec.Body.String()
	if !strings.Contains(body, `<meta http-equiv="refresh" content="60">`) {
		t.Errorf("/ops response missing meta-refresh tag")
	}
}
