package server_test

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/PinchasLev/filings-watcher/service/internal/server"
	"github.com/PinchasLev/filings-watcher/service/internal/store"
)

func TestHandleHomeRendersFilings(t *testing.T) {
	ticker := "ACME"
	item := "2.02"
	fake := &fakeStore{
		eventTypeCountResult: []store.EventTypeCount{
			{EventType: "earnings_release", Count: 12},
			{EventType: "ma_activity", Count: 5},
		},
		materialResult: []store.Classification{
			{
				ID:              1,
				AccessionNumber: "0001234567-26-000001",
				ItemNumber:      &item,
				EventType:       "earnings_release",
				EventDomain:     "financial",
				IsMaterial:      true,
				Confidence:      0.93,
				Reasoning:       "Quarterly results press release furnished as Exhibit 99.1.",
				CompanyName:     "Acme Corp",
				Ticker:          &ticker,
				FilingDate:      "2026-05-20",
			},
		},
		materialTotal: 1,
	}

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/", nil)
	server.New(fake).ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rec.Code)
	}
	if got := rec.Header().Get("Content-Type"); !strings.HasPrefix(got, "text/html") {
		t.Fatalf("expected text/html content-type, got %q", got)
	}
	body := rec.Body.String()
	for _, want := range []string{
		"Filings Radar",                   // app title
		"Acme Corp",                       // company
		"(ACME)",                          // ticker
		"Earnings release",                // pretty event label in filter and card
		"M&amp;A activity",                // pretty label, HTML-escaped
		"All material",                    // default filter chip
		"Showing material events only",    // intro copy
		"93%",                             // confidence formatted as percent
		"Quarterly results press release", // reasoning text
		"https://www.sec.gov/Archives/edgar/data/", // EDGAR link prefix
		"0001234567-26-000001-index.htm",           // EDGAR link suffix
		"Open source on GitHub",                    // footer credit
	} {
		if !strings.Contains(body, want) {
			t.Errorf("expected body to contain %q, body was:\n%s", want, body)
		}
	}
}

func TestHandleHomeFiltersByEventTypeQueryParam(t *testing.T) {
	fake := &fakeStore{
		eventTypeCountResult: []store.EventTypeCount{
			{EventType: "ma_activity", Count: 3},
		},
		materialResult: nil,
		materialTotal:  0,
	}

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/?event=ma_activity", nil)
	server.New(fake).ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rec.Code)
	}
	if fake.materialCalledWith.eventType != "ma_activity" {
		t.Errorf("expected store to be called with eventType=ma_activity, got %q",
			fake.materialCalledWith.eventType)
	}
	if fake.materialCalledWith.limit != 50 {
		t.Errorf("expected limit=50 (the home page default), got %d", fake.materialCalledWith.limit)
	}
	if !strings.Contains(rec.Body.String(), "No classifications match") {
		t.Errorf("expected empty-state copy, got: %s", rec.Body.String())
	}
}

func TestHandleHomePaginationLinks(t *testing.T) {
	// Build 50 filings (one full page) but report a total of 299 so the
	// handler renders both "Newer" (because offset > 0) and "Older"
	// (because more results remain).
	filings := make([]store.Classification, 50)
	for i := range filings {
		filings[i] = store.Classification{
			ID:              int64(i),
			AccessionNumber: "0000000001-26-000001",
			EventType:       "shareholder_vote_results",
			IsMaterial:      true,
			Confidence:      0.95,
			Reasoning:       "vote result",
			CompanyName:     "Co",
			FilingDate:      "2026-05-20",
		}
	}
	fake := &fakeStore{
		eventTypeCountResult: []store.EventTypeCount{
			{EventType: "shareholder_vote_results", Count: 299},
		},
		materialResult: filings,
		materialTotal:  299,
	}

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/?event=shareholder_vote_results&offset=50", nil)
	server.New(fake).ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rec.Code)
	}
	if fake.materialCalledWith.offset != 50 {
		t.Errorf("expected store called with offset=50, got %d", fake.materialCalledWith.offset)
	}
	body := rec.Body.String()
	for _, want := range []string{
		`href="/?event=shareholder_vote_results"`,                // "Newer" link, drops offset to 0
		`href="/?event=shareholder_vote_results&amp;offset=100"`, // "Older" link, offset advances by limit
		"51-100 of 299", // range label
	} {
		if !strings.Contains(body, want) {
			t.Errorf("expected body to contain %q; body was:\n%s", want, body)
		}
	}
}

func TestHandleHomePaginationDisabledWhenSinglePage(t *testing.T) {
	fake := &fakeStore{
		eventTypeCountResult: []store.EventTypeCount{{EventType: "earnings_release", Count: 5}},
		materialResult: []store.Classification{{
			ID: 1, AccessionNumber: "0000000001-26-000001",
			EventType: "earnings_release", IsMaterial: true, Confidence: 0.9,
			Reasoning: "ok", CompanyName: "Co", FilingDate: "2026-05-20",
		}},
		materialTotal: 5,
	}
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/", nil)
	server.New(fake).ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rec.Code)
	}
	body := rec.Body.String()
	if strings.Contains(body, "Newer") || strings.Contains(body, "Older") {
		t.Errorf("expected no pagination controls when total <= limit; got body containing pagination text:\n%s", body)
	}
}

func TestHandleHomeReturns404ForUnknownPath(t *testing.T) {
	fake := &fakeStore{}
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/unknown", nil)
	server.New(fake).ServeHTTP(rec, req)
	if rec.Code != http.StatusNotFound {
		t.Errorf("expected 404 for /unknown, got %d", rec.Code)
	}
}
