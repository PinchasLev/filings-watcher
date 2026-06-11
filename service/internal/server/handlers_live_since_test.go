package server_test

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/PinchasLev/filings-watcher/service/internal/server"
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

// TestHandleLiveEmbedsScriptTagWithRenderedAt confirms the /live page
// renders the <script src="/static/live.js" data-since="..."> tag so the
// poller has a baseline.
func TestHandleLiveEmbedsScriptTagWithRenderedAt(t *testing.T) {
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
