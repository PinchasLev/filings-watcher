package server

import (
	"context"
	"fmt"
	"log/slog"
	"net/http"
	"runtime/debug"
	"time"

	"github.com/PinchasLev/filings-watcher/service/internal/alerts"
)

// RecoverPanic wraps next with panic recovery. A recovered handler panic is
// logged, raised as an operator ALERT (best-effort, ADR 0031), and turned into
// a 500 — so one bad handler surfaces loudly instead of taking the process
// down silently. Composable middleware (applied in main, outside the otel span
// wrapper) rather than baked into New, so the routing constructor stays
// read-only and untouched.
//
// The alert uses a detached, time-bounded context rather than the request's:
// the client may have disconnected (which is sometimes the panic's cause), and
// the alert must be recorded regardless. dedup_key is the route, so a
// hot-looping panic on one path coalesces to one page downstream instead of a
// storm.
func RecoverPanic(emitter alerts.Emitter, next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		defer func() {
			rec := recover()
			if rec == nil {
				return
			}
			// http.ErrAbortHandler is the sentinel a handler panics with to
			// abort silently; respect it rather than alerting on it.
			if rec == http.ErrAbortHandler {
				panic(rec)
			}

			slog.Error("handler panic recovered",
				"panic", rec, "method", r.Method, "path", r.URL.Path,
				"stack", string(debug.Stack()))

			emitCtx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
			defer cancel()
			emitter.Emit(emitCtx, alerts.SeverityAlert, "Handler panic recovered",
				alerts.WithBody(fmt.Sprintf("%s %s panicked: %v", r.Method, r.URL.Path, rec)),
				alerts.WithDedupKey("panic:"+r.URL.Path),
				alerts.WithFields(map[string]any{
					"method": r.Method,
					"path":   r.URL.Path,
					"panic":  fmt.Sprint(rec),
				}),
			)

			w.WriteHeader(http.StatusInternalServerError)
		}()
		next.ServeHTTP(w, r)
	})
}
