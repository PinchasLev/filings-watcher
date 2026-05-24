package server_test

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/PinchasLev/filings-watcher/service/internal/server"
	"github.com/PinchasLev/filings-watcher/service/internal/store"
)

func TestHandleFilingDetailRendersHTMLWhenBrowserAccept(t *testing.T) {
	ticker := "ACME"
	item := "5.02"
	itemTitle := "Departure of Directors or Certain Officers"
	fake := &fakeStore{
		filingResult: &store.FilingDetail{
			Filing: store.Filing{
				AccessionNumber: "0001234567-26-000001",
				CIK:             "0001234567",
				Ticker:          &ticker,
				CompanyName:     "Acme Corp",
				Form:            "8-K",
				FilingDate:      "2026-05-20",
			},
			Classifications: []store.Classification{
				{
					ID:                1,
					AccessionNumber:   "0001234567-26-000001",
					ItemNumber:        &item,
					ItemTitle:         &itemTitle,
					EventType:         "exec_departure",
					IsMaterial:        true,
					Confidence:        0.95,
					Reasoning:         "CFO resignation announced, effective immediately.",
					ClassifierVersion: "claude-haiku-4-5+prompt-abc",
					TaxonomyVersion:   "v1",
					ClassifiedAt:      "2026-05-20T22:00:00Z",
				},
			},
		},
	}

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/filings/0001234567-26-000001", nil)
	req.Header.Set("Accept", "text/html")
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
		"Form 8-K",
		"0001234567-26-000001", // accession in meta
		"0001234567",           // CIK in meta
		"Item 5.02",
		"Departure of Directors",
		"Exec departure", // pretty event label
		"material",       // detail page DOES show the badge (unlike home page)
		"95%",            // confidence
		"CFO resignation announced",
		"Back to all filings", // breadcrumb
	} {
		if !strings.Contains(body, want) {
			t.Errorf("expected body to contain %q", want)
		}
	}
}

func TestHandleFilingDetailReturnsJSONWhenNoHTMLAccept(t *testing.T) {
	fake := &fakeStore{
		filingResult: &store.FilingDetail{
			Filing: store.Filing{
				AccessionNumber: "0001234567-26-000001",
				CIK:             "0001234567",
				CompanyName:     "Acme Corp",
				Form:            "8-K",
				FilingDate:      "2026-05-20",
			},
			Classifications: nil,
		},
	}

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/filings/0001234567-26-000001", nil)
	// No Accept header set — should default to JSON for backwards compatibility.
	server.New(fake).ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rec.Code)
	}
	if got := rec.Header().Get("Content-Type"); !strings.HasPrefix(got, "application/json") {
		t.Fatalf("expected application/json content-type, got %q", got)
	}
	body := rec.Body.String()
	if !strings.Contains(body, `"accession_number":"0001234567-26-000001"`) {
		t.Errorf("expected JSON payload to include accession_number; body was: %s", body)
	}
}

func TestHandleFilingDetailRendersBothMaterialAndNonMaterial(t *testing.T) {
	// The detail page is the comprehensive view: should show non-material
	// classifications too, unlike the home page which filters them out.
	itemMaterial := "5.02"
	itemNonMaterial := "8.01"
	fake := &fakeStore{
		filingResult: &store.FilingDetail{
			Filing: store.Filing{
				AccessionNumber: "0001234567-26-000001",
				CompanyName:     "Acme Corp",
				Form:            "8-K",
				FilingDate:      "2026-05-20",
			},
			Classifications: []store.Classification{
				{
					ItemNumber: &itemMaterial,
					EventType:  "exec_departure",
					IsMaterial: true,
					Confidence: 0.9,
					Reasoning:  "real material event",
				},
				{
					ItemNumber: &itemNonMaterial,
					EventType:  "other_material",
					IsMaterial: false,
					Confidence: 0.7,
					Reasoning:  "routine administrative disclosure",
				},
			},
		},
	}

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/filings/0001234567-26-000001", nil)
	req.Header.Set("Accept", "text/html")
	server.New(fake).ServeHTTP(rec, req)

	body := rec.Body.String()
	if !strings.Contains(body, "real material event") {
		t.Errorf("expected detail page to show material classification reasoning")
	}
	if !strings.Contains(body, "routine administrative disclosure") {
		t.Errorf("expected detail page to ALSO show non-material classification reasoning")
	}
	if !strings.Contains(body, "non-material") {
		t.Errorf("expected explicit 'non-material' badge on non-material rows")
	}
}

func TestHandleFilingDetailNotFoundHTML(t *testing.T) {
	fake := &fakeStore{filingErr: store.ErrNotFound}
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/filings/0000000000-99-999999", nil)
	req.Header.Set("Accept", "text/html")
	server.New(fake).ServeHTTP(rec, req)

	if rec.Code != http.StatusNotFound {
		t.Fatalf("expected 404, got %d", rec.Code)
	}
}
