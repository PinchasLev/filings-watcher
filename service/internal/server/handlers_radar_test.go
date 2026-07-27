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
			Thesis:            "Alpha faces major, broad-based deterioration this year. This second sentence carries detail that belongs on the company page, not the feed.",
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
		"/companies/0000000111#risk-radar",             // deep-link to the section
		"Major worsening",                              // composed headline
		"44 material",                                  // counts
		"Alpha faces major, broad-based deterioration", // the lead sentence
		`href="/radar?intensity=major"`,                // filter chip
		"2025-12-31",                                   // fiscal period
	} {
		if !strings.Contains(body, want) {
			t.Errorf("expected radar body to contain %q", want)
		}
	}
	// The feed shows only the lead sentence; the rest stays on the company page.
	if strings.Contains(body, "belongs on the company page") {
		t.Errorf("feed should show only the first sentence, not the full thesis")
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
