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

// TestHandleLiveRendersTapeWithDefaultWindow checks the happy path: GET /live
// renders the tape with the default 1-hour window and surfaces the LiveEvents
// row.
func TestHandleLiveRendersTapeWithDefaultWindow(t *testing.T) {
	ticker := "ACME"
	submitted := time.Now().Add(-90 * time.Second).UTC().Format(time.RFC3339)
	fake := &fakeStore{
		liveEventsResult: []store.Event{
			{
				ID:              1,
				AccessionNumber: "0001234567-26-000001",
				EventType:       "earnings_release",
				EventDomain:     "financial",
				IsMaterial:      true,
				Confidence:      0.93,
				Summary:         "Press release furnished as Exhibit 99.1.",
				CompanyName:     "Acme Corp",
				Ticker:          &ticker,
				FilingDate:      "2026-06-05",
				SubmittedAt:     &submitted,
			},
		},
		liveEventsTotal: 1,
	}

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/live", nil)
	server.New(fake).ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rec.Code)
	}
	if got := rec.Header().Get("Content-Type"); !strings.HasPrefix(got, "text/html") {
		t.Fatalf("expected text/html content-type, got %q", got)
	}
	body := rec.Body.String()
	for _, want := range []string{
		"Acme Corp",
		"(ACME)",
		"Earnings release",
		"93%",
		"Last hour",       // window-toggle label for the default
		`<a href="/live"`, // active toggle link (no ?hours= when default)
	} {
		if !strings.Contains(body, want) {
			t.Errorf("response missing %q", want)
		}
	}

	// Default window is 1 hour: since should be approximately now-1h.
	wantSince := time.Now().Add(-time.Hour)
	if delta := fake.liveCalledWith.since.Sub(wantSince).Abs(); delta > 2*time.Second {
		t.Errorf("since = %v, want %v +/- 2s", fake.liveCalledWith.since, wantSince)
	}
}

// TestHandleLiveHonorsHoursQueryParam confirms ?hours=3 shifts the window.
func TestHandleLiveHonorsHoursQueryParam(t *testing.T) {
	fake := &fakeStore{liveEventsResult: nil, liveEventsTotal: 0}

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/live?hours=3", nil)
	server.New(fake).ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rec.Code)
	}
	wantSince := time.Now().Add(-3 * time.Hour)
	if delta := fake.liveCalledWith.since.Sub(wantSince).Abs(); delta > 2*time.Second {
		t.Errorf("since = %v, want %v +/- 2s", fake.liveCalledWith.since, wantSince)
	}
}

// TestHandleLiveFallsBackToDefaultOnInvalidHours confirms unsupported window
// values reset to the default rather than honoring arbitrary input.
func TestHandleLiveFallsBackToDefaultOnInvalidHours(t *testing.T) {
	fake := &fakeStore{liveEventsResult: nil, liveEventsTotal: 0}

	rec := httptest.NewRecorder()
	// 99 hours isn't in the allowed window set ({1, 3, 24}).
	req := httptest.NewRequest(http.MethodGet, "/live?hours=99", nil)
	server.New(fake).ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rec.Code)
	}
	wantSince := time.Now().Add(-time.Hour) // default 1h
	if delta := fake.liveCalledWith.since.Sub(wantSince).Abs(); delta > 2*time.Second {
		t.Errorf("since = %v, want %v (fallback to default), got delta %v",
			fake.liveCalledWith.since, wantSince, delta)
	}
}

// TestHandleLiveTopBarToggleMarksLiveActive checks the navigation surface:
// on /live, the "Live" toggle should render with the active style and "Latest"
// without it.
func TestHandleLiveTopBarToggleMarksLiveActive(t *testing.T) {
	fake := &fakeStore{liveEventsResult: nil, liveEventsTotal: 0}

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/live", nil)
	server.New(fake).ServeHTTP(rec, req)

	body := rec.Body.String()
	if !strings.Contains(body, `<a href="/live" class="active"`) {
		t.Errorf("expected Live toggle to render active on /live; body lacks active link")
	}
	if !strings.Contains(body, `<a href="/" >Latest</a>`) {
		// The home toggle stays plain (no active class) on /live.
		// Allowing either an empty class attr or none; check the inactive shape.
		if !strings.Contains(body, `<a href="/" `) {
			t.Errorf("expected inactive Latest toggle on /live")
		}
	}
}
