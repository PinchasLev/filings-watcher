package server_test

import (
	"context"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/PinchasLev/filings-watcher/service/internal/alerts"
	"github.com/PinchasLev/filings-watcher/service/internal/server"
)

type emitCall struct {
	severity string
	title    string
}

// fakeEmitter records Emit calls for assertion. Satisfies alerts.Emitter.
type fakeEmitter struct {
	calls []emitCall
}

func (f *fakeEmitter) Emit(_ context.Context, severity, title string, _ ...alerts.Option) {
	f.calls = append(f.calls, emitCall{severity: severity, title: title})
}

func TestRecoverPanic_RecoversLogsAndAlerts(t *testing.T) {
	panicking := http.HandlerFunc(func(http.ResponseWriter, *http.Request) {
		panic("boom")
	})
	fe := &fakeEmitter{}
	h := server.RecoverPanic(fe, panicking)

	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/live", nil))

	if rec.Code != http.StatusInternalServerError {
		t.Fatalf("status = %d, want 500", rec.Code)
	}
	if len(fe.calls) != 1 {
		t.Fatalf("emit calls = %d, want 1", len(fe.calls))
	}
	if fe.calls[0].severity != alerts.SeverityAlert {
		t.Fatalf("severity = %q, want %q", fe.calls[0].severity, alerts.SeverityAlert)
	}
	if fe.calls[0].title != "Handler panic recovered" {
		t.Fatalf("title = %q", fe.calls[0].title)
	}
}

func TestRecoverPanic_PassesThroughWhenNoPanic(t *testing.T) {
	ok := http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("ok"))
	})
	fe := &fakeEmitter{}
	h := server.RecoverPanic(fe, ok)

	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/", nil))

	if rec.Code != http.StatusOK || rec.Body.String() != "ok" {
		t.Fatalf("status/body = %d/%q", rec.Code, rec.Body.String())
	}
	if len(fe.calls) != 0 {
		t.Fatalf("no panic should not alert, got %d calls", len(fe.calls))
	}
}

func TestRecoverPanic_RepanicsOnErrAbortHandlerWithoutAlerting(t *testing.T) {
	aborting := http.HandlerFunc(func(http.ResponseWriter, *http.Request) {
		panic(http.ErrAbortHandler)
	})
	fe := &fakeEmitter{}
	h := server.RecoverPanic(fe, aborting)

	defer func() {
		r := recover()
		if r != http.ErrAbortHandler {
			t.Fatalf("expected ErrAbortHandler to propagate, got %v", r)
		}
		if len(fe.calls) != 0 {
			t.Fatalf("must not alert on ErrAbortHandler, got %d calls", len(fe.calls))
		}
	}()

	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/", nil))
	t.Fatal("expected ServeHTTP to re-panic with ErrAbortHandler")
}
