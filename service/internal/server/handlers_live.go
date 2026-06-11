// Server-rendered live tape (/live). Material classifications ordered by
// precise EDGAR-side submission time within a rolling window, configurable
// via ?hours=N (default 1; clamped to {1, 3, 24}).
//
// Implicitly atom-feed-only: the LiveEvents store query filters
// `submitted_at IS NOT NULL`, which only the atom ingest path populates.
// Daily-index reconciled rows lack sub-day timestamps and don't belong
// on a "right now" view.
//
// Pure HTML, no JavaScript. A future iteration can add a "X new filings
// since you loaded — refresh" banner, gated on relaxing the CSP from
// script-src 'none' to script-src 'self' in the Caddyfile. Until then,
// freshness is the operator's refresh button + the no-store cache header
// already in place.

package server

import (
	"fmt"
	"html/template"
	"net/http"
	"strconv"
	"time"

	"github.com/PinchasLev/filings-watcher/service/internal/store"
)

const livePageLimit = 50

// Allowed window sizes for ?hours=. The list is short on purpose: each is a
// click in the window-toggle nav, so the surface is fixed. Anything else
// falls back to the default. Order matters — controls the toggle render order.
var liveWindowOptions = []int{1, 3, 24}

const liveDefaultWindowHours = 1

var liveTemplate = template.Must(template.New("layout.html.tmpl").Funcs(templateFuncs).ParseFS(
	templateFS, "templates/layout.html.tmpl", "templates/live.html.tmpl",
))

type livePageData struct {
	Nav           string
	WindowHours   int
	WindowOptions []int
	SinceUTC      string
	Events        []store.Event
	FilteredTotal int
	RangeStart    int
	RangeEnd      int
	PrevURL       string
	NextURL       string
}

func handleLive(s storer) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		hours := parseHours(r.URL.Query().Get("hours"))
		offset := parseOffset(r.URL.Query().Get("offset"))
		since := time.Now().Add(-time.Duration(hours) * time.Hour)

		events, filteredTotal, err := s.LiveEvents(r.Context(), since, livePageLimit, offset)
		if err != nil {
			http.Error(w, "query failed", http.StatusInternalServerError)
			return
		}

		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		if err := liveTemplate.ExecuteTemplate(w, "layout.html.tmpl", livePageData{
			Nav:           "live",
			WindowHours:   hours,
			WindowOptions: liveWindowOptions,
			SinceUTC:      since.UTC().Format(time.RFC3339),
			Events:        events,
			FilteredTotal: filteredTotal,
			RangeStart:    pageRangeStart(offset, len(events)),
			RangeEnd:      pageRangeEnd(offset, len(events)),
			PrevURL:       liveURL(hours, offset-livePageLimit, offset > 0),
			NextURL:       liveURL(hours, offset+livePageLimit, offset+livePageLimit < filteredTotal),
		}); err != nil {
			_ = err
		}
	}
}

// parseHours reads ?hours= and snaps to the allowed window options.
// Unparseable values, negatives, and unsupported values all fall back to
// the default — the toggle UI never produces anything else, so it's safe
// to treat oddities as user error and reset to a known good state.
func parseHours(raw string) int {
	if raw == "" {
		return liveDefaultWindowHours
	}
	n, err := strconv.Atoi(raw)
	if err != nil || n <= 0 {
		return liveDefaultWindowHours
	}
	for _, allowed := range liveWindowOptions {
		if n == allowed {
			return n
		}
	}
	return liveDefaultWindowHours
}

// liveURL composes a /live pagination URL with the current window preserved.
// Empty string when disabled — the template treats that as "render disabled."
func liveURL(hours int, targetOffset int, enabled bool) string {
	if !enabled || targetOffset < 0 {
		return ""
	}
	out := "/live"
	first := true
	if hours != liveDefaultWindowHours {
		out += "?hours=" + strconv.Itoa(hours)
		first = false
	}
	if targetOffset > 0 {
		if first {
			out += "?"
		} else {
			out += "&"
		}
		out += fmt.Sprintf("offset=%d", targetOffset)
	}
	return out
}

// liveWindowURL composes the URL for a window-toggle link. Resets offset
// because changing windows from a deep page rarely makes sense to preserve.
func liveWindowURL(hours int) string {
	if hours == liveDefaultWindowHours {
		return "/live"
	}
	return "/live?hours=" + strconv.Itoa(hours)
}
