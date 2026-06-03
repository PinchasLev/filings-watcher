package server_test

import (
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/PinchasLev/filings-watcher/service/internal/server"
	"github.com/PinchasLev/filings-watcher/service/internal/store"
)

// errSimulated stands in for a non-sentinel store failure (anything that
// isn't store.ErrNotFound), exercising the 500 paths.
var errSimulated = errors.New("simulated store failure")

func TestTickerSearchRedirectsToCompany(t *testing.T) {
	fake := &fakeStore{lookupCIKResult: "0000320193"}

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/?ticker=aapl", nil)
	server.New(fake).ServeHTTP(rec, req)

	if rec.Code != http.StatusFound {
		t.Fatalf("expected 302, got %d", rec.Code)
	}
	if got := rec.Header().Get("Location"); got != "/companies/0000320193" {
		t.Fatalf("expected redirect to /companies/0000320193, got %q", got)
	}
	// The handler forwards the raw symbol; the store does the uppercasing.
	if fake.lookupCalledWith != "aapl" {
		t.Errorf("expected lookup with raw 'aapl', got %q", fake.lookupCalledWith)
	}
}

func TestTickerSearchNotFoundRendersNotice(t *testing.T) {
	fake := &fakeStore{
		lookupCIKErr:         store.ErrNotFound,
		eventTypeCountResult: []store.EventTypeCount{{EventType: "ma_activity", Count: 3}},
		materialEventsResult: nil,
		materialEventsTotal:  0,
	}

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/?ticker=zzzz", nil)
	server.New(fake).ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200 (home with notice), got %d", rec.Code)
	}
	body := rec.Body.String()
	for _, want := range []string{
		"No company found for ticker",
		"ZZZZ", // uppercased echo of the searched symbol
	} {
		if !strings.Contains(body, want) {
			t.Errorf("expected body to contain %q, body was:\n%s", want, body)
		}
	}
}

func TestTickerSearchStoreErrorIs500(t *testing.T) {
	fake := &fakeStore{lookupCIKErr: errSimulated}

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/?ticker=aapl", nil)
	server.New(fake).ServeHTTP(rec, req)

	if rec.Code != http.StatusInternalServerError {
		t.Fatalf("expected 500 on lookup error, got %d", rec.Code)
	}
}

func TestCompanyPageRenders(t *testing.T) {
	ticker := "ACME"
	anchor := "2.02"
	fake := &fakeStore{
		companyResult: &store.Company{
			CIK:         "0001234567",
			Ticker:      "ACME",
			CompanyName: "Acme Corp",
		},
		companyEvents: []store.Event{
			{
				AccessionNumber:  "0001234567-26-000001",
				AnchorItemNumber: &anchor,
				EventType:        "earnings_release",
				IsMaterial:       true,
				Confidence:       0.91,
				Summary:          "Quarterly results press release furnished as Exhibit 99.1.",
				CompanyName:      "Acme Corp",
				Ticker:           &ticker,
				FilingDate:       "2026-05-20",
			},
		},
		companyTotal: 1,
	}

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/companies/0001234567", nil)
	server.New(fake).ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rec.Code)
	}
	if fake.companyCalledWith.cik != "0001234567" {
		t.Errorf("expected CompanyByCIK called with cik 0001234567, got %q", fake.companyCalledWith.cik)
	}
	body := rec.Body.String()
	for _, want := range []string{
		"Acme Corp",
		"(ACME)",
		"CIK 0001234567",
		"1 material event",
		"Earnings release",
		"0001234567-26-000001-index.htm", // EDGAR link to the filing
		"/filings/0001234567-26-000001",  // link into the filing detail
	} {
		if !strings.Contains(body, want) {
			t.Errorf("expected body to contain %q, body was:\n%s", want, body)
		}
	}
}

func TestCompanyPageNotFoundIs404(t *testing.T) {
	fake := &fakeStore{companyErr: store.ErrNotFound}

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/companies/9999999999", nil)
	server.New(fake).ServeHTTP(rec, req)

	if rec.Code != http.StatusNotFound {
		t.Fatalf("expected 404 for unknown CIK, got %d", rec.Code)
	}
}

func TestCompanyPageKnownButEmptyRendersTrackedState(t *testing.T) {
	fake := &fakeStore{
		companyResult: &store.Company{
			CIK:         "0001234567",
			Ticker:      "ACME",
			CompanyName: "Acme Corp",
		},
		companyEvents: nil,
		companyTotal:  0,
	}

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/companies/0001234567", nil)
	server.New(fake).ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200 for tracked-but-empty company, got %d", rec.Code)
	}
	body := rec.Body.String()
	if !strings.Contains(body, "none of its filings have been classified") {
		t.Errorf("expected tracked-but-empty notice, body was:\n%s", body)
	}
}

func TestCompanyPageStoreErrorIs500(t *testing.T) {
	fake := &fakeStore{companyErr: errSimulated}

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/companies/0001234567", nil)
	server.New(fake).ServeHTTP(rec, req)

	if rec.Code != http.StatusInternalServerError {
		t.Fatalf("expected 500 on company query error, got %d", rec.Code)
	}
}
