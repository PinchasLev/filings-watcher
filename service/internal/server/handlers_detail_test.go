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
	anchor := "5.02"
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
		},
		eventsResult: []store.EventWithItems{
			{
				Event: store.Event{
					AccessionNumber:  "0001234567-26-000001",
					AnchorItemNumber: &anchor,
					EventType:        "exec_departure",
					IsMaterial:       true,
					Confidence:       0.95,
					Summary:          "CFO resignation announced, effective immediately.",
				},
				Items: []store.Classification{
					{
						ID:                1,
						AccessionNumber:   "0001234567-26-000001",
						ItemNumber:        &item,
						ItemTitle:         &itemTitle,
						EventType:         "exec_departure",
						IsMaterial:        true,
						Confidence:        0.95,
						Reasoning:         "Item 5.02 reports the CFO departure.",
						ClassifierVersion: "claude-haiku-4-5+prompt-abc",
						TaxonomyVersion:   "v1",
						ClassifiedAt:      "2026-05-20T22:00:00Z",
					},
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
		"0001234567-26-000001",      // accession in meta
		"0001234567",                // CIK in meta
		"Exec departure",            // pretty event label on the event card
		"material",                  // detail page DOES show the badge (unlike home page)
		"95%",                       // confidence
		"CFO resignation announced", // event summary
		"Back to all filings",       // breadcrumb
	} {
		if !strings.Contains(body, want) {
			t.Errorf("expected body to contain %q", want)
		}
	}
	// Single-Item events render the event card alone — no drill-down disclosure.
	// The reduce stage was a pass-through, so the Item's reasoning is the event
	// summary; there is nothing new to reveal. (The anchor Item number — "Item
	// 5.02" — appears as event-card metadata regardless, so it is NOT a
	// drill-down-only signal; the nested Item title and reasoning are.)
	for _, unwanted := range []string{
		"Show the",                  // disclosure wording
		"source Items",              // disclosure wording
		"Departure of Directors",    // nested Item title (drill-down only)
		"Item 5.02 reports the CFO", // nested Item reasoning (drill-down only)
	} {
		if strings.Contains(body, unwanted) {
			t.Errorf("expected single-Item event to render without drill-down; body contained %q", unwanted)
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

func TestHandleFilingDetailDrillDownShowsMaterialAndNonMaterialItems(t *testing.T) {
	// The detail page's per-event drill-down is the comprehensive view: an
	// event expands to ALL the Items it collated, including non-material
	// companion Items (e.g. a Reg-FD furnishing), each with its own badge.
	anchor := "5.02"
	itemMaterial := "5.02"
	itemNonMaterial := "7.01"
	fake := &fakeStore{
		filingResult: &store.FilingDetail{
			Filing: store.Filing{
				AccessionNumber: "0001234567-26-000001",
				CompanyName:     "Acme Corp",
				Form:            "8-K",
				FilingDate:      "2026-05-20",
			},
		},
		eventsResult: []store.EventWithItems{
			{
				Event: store.Event{
					AccessionNumber:  "0001234567-26-000001",
					AnchorItemNumber: &anchor,
					EventType:        "exec_departure",
					IsMaterial:       true,
					Confidence:       0.9,
					Summary:          "CFO departure with an accompanying press release.",
				},
				Items: []store.Classification{
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
		},
	}

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/filings/0001234567-26-000001", nil)
	req.Header.Set("Accept", "text/html")
	server.New(fake).ServeHTTP(rec, req)

	body := rec.Body.String()
	if !strings.Contains(body, "Show the 2 source Items") {
		t.Errorf("expected the drill-down disclosure to advertise both source Items")
	}
	if !strings.Contains(body, "real material event") {
		t.Errorf("expected drill-down to show the material Item's reasoning")
	}
	if !strings.Contains(body, "routine administrative disclosure") {
		t.Errorf("expected drill-down to ALSO show the non-material Item's reasoning")
	}
	if !strings.Contains(body, "non-material") {
		t.Errorf("expected explicit 'non-material' badge on the non-material Item")
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
