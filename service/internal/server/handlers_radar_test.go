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
		radarTotal:        1,
		radarTracked:      392,
		radarWithSpecific: 118,
		radarRows: []store.RiskRadarRow{{
			CIK:           "0000000111",
			CompanyName:   "Alpha Corporation",
			Ticker:        "ALPH",
			Accession:     "0000000111-26-000001",
			CurrentPeriod: "2025-12-31",
			FiledAt:       "2026-03-01",
			SpecificCount: 2,
			TopSpecific:   []string{"Lost its largest customer.", "Announced a merger."},
			Thesis:        "Alpha faces broad-based change this year.",
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
		"Tracking 392 companies", "118 with company-specific changes", // coverage line
		"ALPH", "Alpha Corporation", // company identity
		"/companies/0000000111#risk-radar", // deep-link to the section
		"2 company-specific changes",       // the specific count
		"Lost its largest customer.",       // a top specific caption
		"Announced a merger.",              // a top specific caption
		"2025-12-31",                       // fiscal period
	} {
		if !strings.Contains(body, want) {
			t.Errorf("expected radar body to contain %q", want)
		}
	}
	// The tone-based intensity filter chips are gone.
	if strings.Contains(body, "intensity=") {
		t.Errorf("radar should no longer carry intensity filter chips")
	}
}

func TestFirstSentence(t *testing.T) {
	cases := map[string]string{
		"A short lead sentence here. And a second one.": "A short lead sentence here.",
		"No terminal punctuation at all":                "No terminal punctuation at all",
		// An early abbreviation ("U.S.") must not truncate the lead sentence.
		"The U.S. economy weakened and margins compressed sharply. More detail follows here.": "The U.S. economy weakened and margins compressed sharply.",
		// A decimal is safe (period followed by a digit, not a space).
		"Shares fell below $1.00 and a delisting notice followed within the quarter. Next.": "Shares fell below $1.00 and a delisting notice followed within the quarter.",
	}
	for in, want := range cases {
		if got := server.FirstSentenceForTest(in); got != want {
			t.Errorf("firstSentence(%q) = %q, want %q", in, got, want)
		}
	}
}

func TestRadarPageEmptyState(t *testing.T) {
	fake := &fakeStore{radarRows: nil}
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/radar", nil)
	server.New(fake).ServeHTTP(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rec.Code)
	}
	if !strings.Contains(rec.Body.String(), "No company-specific disclosure changes on record") {
		t.Errorf("expected empty-state copy for a feed with no rows")
	}
}
