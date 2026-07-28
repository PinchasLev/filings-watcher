package server_test

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/PinchasLev/filings-watcher/service/internal/server"
)

func TestQueryConsoleRendersFormAndExamples(t *testing.T) {
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/ops/query", nil)
	server.New(&fakeStore{}).ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rec.Code)
	}
	body := rec.Body.String()
	for _, want := range []string{
		"query console", "<textarea", "Run",
		"Recent views",    // an example label
		"/ops/query?sql=", // example links carry the SQL
	} {
		if !strings.Contains(body, want) {
			t.Errorf("expected console body to contain %q", want)
		}
	}
}

func TestQueryConsoleRendersResults(t *testing.T) {
	fake := &fakeStore{
		queryCols: []string{"path", "n"},
		queryRows: [][]string{{"/radar", "120"}, {"/", "90"}},
	}
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/ops/query?sql=SELECT+path", nil)
	server.New(fake).ServeHTTP(rec, req)

	body := rec.Body.String()
	for _, want := range []string{"path", "/radar", "120", "2 rows"} {
		if !strings.Contains(body, want) {
			t.Errorf("expected results body to contain %q", want)
		}
	}
}

func TestQueryConsoleShowsError(t *testing.T) {
	fake := &fakeStore{queryErr: errString("only read-only SELECT / WITH queries are allowed")}
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/ops/query?sql=DELETE+FROM+page_views", nil)
	server.New(fake).ServeHTTP(rec, req)

	if !strings.Contains(rec.Body.String(), "only read-only") {
		t.Errorf("expected the query error to be shown")
	}
}

type errString string

func (e errString) Error() string { return string(e) }
