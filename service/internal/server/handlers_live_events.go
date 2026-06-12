// HTML-fragment endpoint that backs the /live tape's auto-prepend.
// Returns concatenated `filing-card` partials for material atom-
// ingested events newer than ?since=, sorted DESC. The /live page's
// JS calls this every 30 seconds; the response is inserted at the
// top of #live-tape with insertAdjacentHTML('afterbegin', ...). One
// shared `filing-card` template renders both the initial server
// render and these fragments, so the card markup has a single source
// of truth.
//
// Cache-Control: no-store is set so the response can't be stale —
// freshness is the entire point.
//
// The maximum number of cards per response is bounded by
// liveEventsPerPoll to keep the worst-case payload (e.g. operator
// re-opens an old tab) finite. A poll that hits the cap just means
// the next poll picks up where this one left off.

package server

import (
	"bytes"
	"net/http"
	"time"
)

const liveEventsPerPoll = 50

func handleLiveEvents(s storer) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		raw := r.URL.Query().Get("since")
		if raw == "" {
			http.Error(w, "missing ?since=", http.StatusBadRequest)
			return
		}
		since, err := time.Parse(time.RFC3339, raw)
		if err != nil {
			http.Error(w, "invalid ?since= (expected RFC3339)", http.StatusBadRequest)
			return
		}

		events, err := s.ListLiveEventsSince(r.Context(), since, liveEventsPerPoll)
		if err != nil {
			http.Error(w, "query failed", http.StatusInternalServerError)
			return
		}

		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		w.Header().Set("Cache-Control", "no-store")
		if len(events) == 0 {
			return
		}
		var buf bytes.Buffer
		for _, ev := range events {
			if err := liveTemplate.ExecuteTemplate(&buf, "filing-card", ev); err != nil {
				http.Error(w, "render failed", http.StatusInternalServerError)
				return
			}
		}
		_, _ = w.Write(buf.Bytes())
	}
}
