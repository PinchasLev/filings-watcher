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

// TestHandleLiveEventsHappyPath: valid since, store returns events, the
// response is HTML containing the rendered filing-card markup.
func TestHandleLiveEventsHappyPath(t *testing.T) {
	ts := "2026-06-11T15:35:00-04:00"
	fake := &fakeStore{
		listLiveSinceResult: []store.Event{
			{
				AccessionNumber: "0001234567-26-000001",
				EventType:       "earnings_release",
				IsMaterial:      true,
				Confidence:      0.93,
				Summary:         "Quarterly results press release.",
				CompanyName:     "Acme Corp",
				FilingDate:      "2026-06-11",
				SubmittedAt:     &ts,
			},
		},
	}

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/api/live-events?since=2026-06-11T15:00:00Z", nil)
	server.New(fake).ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200", rec.Code)
	}
	if got := rec.Header().Get("Content-Type"); !strings.HasPrefix(got, "text/html") {
		t.Errorf("Content-Type = %q, want text/html", got)
	}
	if got := rec.Header().Get("Cache-Control"); got != "no-store" {
		t.Errorf("Cache-Control = %q, want no-store", got)
	}

	body := rec.Body.String()
	for _, want := range []string{
		`<article class="filing-card"`,
		"Acme Corp",
		"Earnings release",
		`<time class="submitted-at" datetime="` + ts + `"`,
	} {
		if !strings.Contains(body, want) {
			t.Errorf("response missing %q", want)
		}
	}
}

// TestHandleLiveEventsEmptyResultIsEmptyBody: store returns no events,
// the body is empty (no card markup). JS treats empty as no-op.
func TestHandleLiveEventsEmptyResultIsEmptyBody(t *testing.T) {
	fake := &fakeStore{listLiveSinceResult: nil}
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/api/live-events?since=2026-06-11T15:00:00Z", nil)
	server.New(fake).ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200", rec.Code)
	}
	if rec.Body.Len() != 0 {
		t.Errorf("body = %q, want empty", rec.Body.String())
	}
}

// TestHandleLiveEventsMissingSince: empty ?since= → 400.
func TestHandleLiveEventsMissingSince(t *testing.T) {
	fake := &fakeStore{}
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/api/live-events", nil)
	server.New(fake).ServeHTTP(rec, req)

	if rec.Code != http.StatusBadRequest {
		t.Errorf("status = %d, want 400", rec.Code)
	}
}

// TestHandleLiveEventsInvalidSince: malformed ?since= → 400.
func TestHandleLiveEventsInvalidSince(t *testing.T) {
	fake := &fakeStore{}
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/api/live-events?since=yesterday", nil)
	server.New(fake).ServeHTTP(rec, req)

	if rec.Code != http.StatusBadRequest {
		t.Errorf("status = %d, want 400", rec.Code)
	}
}

// TestHandleLiveEventsPassesParsedTimeAndLimit confirms the parsed time
// flows correctly to ListLiveEventsSince along with the per-poll cap.
func TestHandleLiveEventsPassesParsedTimeAndLimit(t *testing.T) {
	fake := &fakeStore{}
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/api/live-events?since=2026-06-11T15:00:00Z", nil)
	server.New(fake).ServeHTTP(rec, req)

	want, _ := time.Parse(time.RFC3339, "2026-06-11T15:00:00Z")
	if len(fake.listLiveSinceCalledWith) != 1 || !fake.listLiveSinceCalledWith[0].Equal(want) {
		t.Errorf("ListLiveEventsSince since = %v, want %v", fake.listLiveSinceCalledWith, want)
	}
	if len(fake.listLiveSinceLimitWith) != 1 || fake.listLiveSinceLimitWith[0] <= 0 {
		t.Errorf("ListLiveEventsSince limit = %v, want > 0", fake.listLiveSinceLimitWith)
	}
}

// TestHandleLiveScriptServesEmbeddedFile checks the /static/live.js route
// returns the embedded JS with the right Content-Type and the strings
// the script must contain to do its job.
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
		"/api/live-events", // endpoint the script polls
		"data-since",       // baseline attribute
		"live-tape",        // container id where cards are prepended
		"toLocaleString",   // timezone localization
	} {
		if !strings.Contains(body, want) {
			t.Errorf("served live.js missing %q", want)
		}
	}
}

// TestHandleLiveEmbedsScriptTagAndTimeElement confirms the /live page
// renders the script tag and uses <time datetime> markup for the
// timestamp on each card.
func TestHandleLiveEmbedsScriptTagAndTimeElement(t *testing.T) {
	ts := "2026-06-11T17:30:00-04:00"
	fake := &fakeStore{
		liveEventsResult: []store.Event{{
			AccessionNumber: "0001-26-001",
			IsMaterial:      true,
			CompanyName:     "Sample Co.",
			SubmittedAt:     &ts,
			FilingDate:      "2026-06-11",
		}},
		liveEventsTotal: 1,
	}

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/live", nil)
	server.New(fake).ServeHTTP(rec, req)

	body := rec.Body.String()
	if !strings.Contains(body, `src="/static/live.js"`) {
		t.Errorf("/live response missing live.js script tag")
	}
	if !strings.Contains(body, `<time class="submitted-at" datetime="`+ts+`"`) {
		t.Errorf("/live response missing <time> element with datetime=%q", ts)
	}
	// UTC fallback text should also be present so non-JS viewers see
	// an honest timestamp.
	if !strings.Contains(body, "2026-06-11 21:30 UTC") {
		t.Errorf("/live response missing UTC fallback text inside <time>")
	}
}

// TestHandleLivePollSinceIsNewestVisibleEventTimestamp confirms the
// poll baseline (the data-since attribute on the script tag) is anchored
// to the newest visible event's submitted_at — so the AJAX poll only
// asks for what hasn't been rendered yet.
func TestHandleLivePollSinceIsNewestVisibleEventTimestamp(t *testing.T) {
	newest := "2026-06-11T17:30:43-04:00"
	older := "2026-06-11T14:00:00-04:00"
	fake := &fakeStore{
		liveEventsResult: []store.Event{
			{
				AccessionNumber: "0001-26-001",
				IsMaterial:      true,
				SubmittedAt:     &newest, // sorted DESC: index 0 is newest
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
	want := `data-since="` + newest + `"`
	if !strings.Contains(body, want) {
		t.Errorf("/live response missing %q", want)
	}
}

// TestHandleLivePollSinceFallsBackToNowOnEmptyWindow: when no events
// render, the data-since attribute must still be a valid timestamp so
// the script's first poll has a meaningful anchor.
func TestHandleLivePollSinceFallsBackToNowOnEmptyWindow(t *testing.T) {
	fake := &fakeStore{liveEventsResult: nil, liveEventsTotal: 0}

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/live", nil)
	server.New(fake).ServeHTTP(rec, req)

	body := rec.Body.String()
	if !strings.Contains(body, `data-since="`) {
		t.Errorf("/live response missing data-since attribute on empty window")
	}
	thisYear := time.Now().UTC().Format("2006")
	if !strings.Contains(body, `data-since="`+thisYear) {
		t.Errorf("expected data-since to start with current year %q", thisYear)
	}
}
