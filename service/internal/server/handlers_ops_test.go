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

// TestHandleOpsRendersBothPanels checks the happy path: GET /ops/ renders
// the spend and freshness panels with the values from the store fakes.
func TestHandleOpsRendersBothPanels(t *testing.T) {
	freshness := time.Now().Add(-5 * time.Minute).UTC().Format(time.RFC3339)
	fake := &fakeStore{
		todaySpendResult: store.SpendSnapshot{TotalUSD: 1.50, CallCount: 42},
		freshnessResult:  &freshness,
	}

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/ops/", nil)
	server.New(fake).ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rec.Code)
	}
	body := rec.Body.String()
	for _, want := range []string{
		"$1.50",
		"of $5.00",
		"42 LLM calls",
		"30.0% of cap",
		"min ago", // relTime rendering of the 5-minute-old freshness
	} {
		if !strings.Contains(body, want) {
			t.Errorf("response missing %q", want)
		}
	}
}

// TestHandleOpsHandlesNoFreshnessData verifies the "no data" branch when
// the corpus has no atom-ingested rows.
func TestHandleOpsHandlesNoFreshnessData(t *testing.T) {
	fake := &fakeStore{
		todaySpendResult: store.SpendSnapshot{TotalUSD: 0.0, CallCount: 0},
		freshnessResult:  nil,
	}

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/ops/", nil)
	server.New(fake).ServeHTTP(rec, req)

	body := rec.Body.String()
	if !strings.Contains(body, "no data") {
		t.Errorf("expected freshness panel to render 'no data' when timestamp is nil")
	}
	if !strings.Contains(body, "$0.00") {
		t.Errorf("expected spend panel to render $0.00 when total is 0")
	}
}

// TestHandleOpsSurfacesPercentBands confirms the warn/alarm CSS classes
// kick in at 60% and 80% of cap respectively.
func TestHandleOpsSurfacesPercentBands(t *testing.T) {
	cases := []struct {
		name      string
		total     float64
		wantClass string
	}{
		{"green band", 1.00, ""}, // 20% of cap — plain bar
		{"warn band", 3.50, "warn"},
		{"alarm band", 4.50, "alarm"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			fake := &fakeStore{
				todaySpendResult: store.SpendSnapshot{TotalUSD: tc.total, CallCount: 1},
				freshnessResult:  nil,
			}
			rec := httptest.NewRecorder()
			req := httptest.NewRequest(http.MethodGet, "/ops/", nil)
			server.New(fake).ServeHTTP(rec, req)

			body := rec.Body.String()
			if tc.wantClass == "" {
				if strings.Contains(body, `class="ops-bar warn"`) || strings.Contains(body, `class="ops-bar alarm"`) {
					t.Errorf("green band: expected no warn/alarm class")
				}
			} else if !strings.Contains(body, `class="ops-bar `+tc.wantClass+`"`) {
				t.Errorf("%s: expected class 'ops-bar %s' in body", tc.name, tc.wantClass)
			}
		})
	}
}
