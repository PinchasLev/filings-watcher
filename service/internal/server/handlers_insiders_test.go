package server_test

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/PinchasLev/filings-watcher/service/internal/server"
	"github.com/PinchasLev/filings-watcher/service/internal/store"
)

func TestHandleInsiders_RendersClusters(t *testing.T) {
	fake := &fakeStore{notableClusters: []store.InsiderCluster{{
		CIK: "0000000111", Ticker: "ACME", CompanyName: "Acme Inc",
		Buyers: 3, Trades: 4, TotalValue: 250000, FirstDate: "2026-06-01", LastDate: "2026-06-05",
	}}}
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/insiders", nil)
	server.New(fake).ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200", rec.Code)
	}
	body := rec.Body.String()
	for _, want := range []string{"ACME", "3 insiders", "$250K", "/companies/0000000111"} {
		if !strings.Contains(body, want) {
			t.Errorf("insiders feed body missing %q", want)
		}
	}
}

func TestHandleInsiders_EmptyState(t *testing.T) {
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/insiders", nil)
	server.New(&fakeStore{}).ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200", rec.Code)
	}
	if !strings.Contains(rec.Body.String(), "No insider cluster buys") {
		t.Errorf("empty-state message not rendered")
	}
}
