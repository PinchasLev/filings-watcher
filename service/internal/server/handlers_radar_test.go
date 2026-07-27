package server_test

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/PinchasLev/filings-watcher/service/internal/server"
	"github.com/PinchasLev/filings-watcher/service/internal/store"
)

func TestRadarPageRendersFeed(t *testing.T) {
	fake := &fakeStore{
		radarCounts: store.RiskRadarCounts{Total: 2, Major: 1, Minor: 1},
		radarTotal:  1,
		radarRows: []store.RiskRadarRow{{
			CIK:               "0000000111",
			CompanyName:       "Alpha Corporation",
			Ticker:            "ALPH",
			Accession:         "0000000111-26-000001",
			CurrentPeriod:     "2025-12-31",
			FiledAt:           "2026-03-01",
			HeadlineDirection: "worsening",
			HeadlineIntensity: "major",
			MaterialCount:     44,
			WorseCount:        35,
			EasedCount:        9,
			Thesis:            "Alpha faces major, broad-based deterioration.",
		}},
	}

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/radar", nil)
	server.New(fake).ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rec.Code)
	}
	body := rec.Body.String()
	for _, want := range []string{
		"Risk Radar",
		"ALPH", "Alpha Corporation", // company identity
		"/companies/0000000111",          // link through to the per-company page
		"Major worsening",                // composed headline
		"44 material",                    // counts
		"Alpha faces major, broad-based", // thesis
		`href="/radar?intensity=major"`,  // filter chip
		"2025-12-31",                     // fiscal period
	} {
		if !strings.Contains(body, want) {
			t.Errorf("expected radar body to contain %q", want)
		}
	}
}

func TestRadarPageInvalidIntensityIsIgnored(t *testing.T) {
	fake := &fakeStore{radarCounts: store.RiskRadarCounts{}, radarRows: nil}
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/radar?intensity=bogus", nil)
	server.New(fake).ServeHTTP(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rec.Code)
	}
	// The "All" chip is active (invalid intensity coerced to ""), and the empty
	// state renders rather than an error.
	if !strings.Contains(rec.Body.String(), "No disclosure changes on record") {
		t.Errorf("expected empty-state copy for a feed with no rows")
	}
}
