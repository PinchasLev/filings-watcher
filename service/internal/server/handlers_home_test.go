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
		"filings-watcher",                 // app title
		"Acme Corp",                       // company
		"(ACME)",                          // ticker
		"Earnings release",                // pretty event label in filter and card
		"M&amp;A activity",                // pretty label, HTML-escaped
		"All material",                    // default filter chip
		"Quarterly results press release", // reasoning text
		"https://www.sec.gov/Archives/edgar/data/", // EDGAR link prefix
		"0001234567-26-000001-index.htm",           // EDGAR link suffix
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

func TestHandleHomeReturns404ForUnknownPath(t *testing.T) {
	fake := &fakeStore{}
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/unknown", nil)
	server.New(fake).ServeHTTP(rec, req)
	if rec.Code != http.StatusNotFound {
		t.Errorf("expected 404 for /unknown, got %d", rec.Code)
	}
}
