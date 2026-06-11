// JSON freshness-poll endpoint that backs the /live tape's "X new
// filings since you loaded — refresh" banner. Counts material atom-
// ingested events whose submission timestamp is strictly after the
// query-string `since`. Strict ">" so the boundary event the page
// rendered with doesn't re-trigger the banner on every poll.
//
// Cache-Control: no-store is set so a browser cache between the JS and
// the endpoint can't stale the count. Caddy's apex block already does
// no-store globally, but this endpoint's freshness is even more
// critical so the header is also set at the Go layer as defense in
// depth.
//
// Pairs with /static/live.js below — the script reads its baseline
// timestamp from a data-since attribute the live template renders.

package server

import (
	"embed"
	"encoding/json"
	"io/fs"
	"net/http"
	"time"
)

//go:embed static/live.js
var staticFS embed.FS

type liveSinceResponse struct {
	NewCount int    `json:"new_count"`
	Now      string `json:"now"` // server-side "now" so the JS can re-baseline if it ever needs to
}

func handleLiveSince(s storer) http.HandlerFunc {
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

		n, err := s.CountLiveEventsSince(r.Context(), since)
		if err != nil {
			http.Error(w, "query failed", http.StatusInternalServerError)
			return
		}

		w.Header().Set("Content-Type", "application/json; charset=utf-8")
		w.Header().Set("Cache-Control", "no-store")
		_ = json.NewEncoder(w).Encode(liveSinceResponse{
			NewCount: n,
			Now:      time.Now().UTC().Format(time.RFC3339),
		})
	}
}

// handleLiveScript serves the embedded /static/live.js. Kept on the Go
// side rather than fronted by Caddy because the script is small,
// rarely-changing, and already in the binary — one fewer file to
// distribute. The handler explicitly sets Content-Type so browsers
// don't fall back to the embed's default detection.
func handleLiveScript() http.HandlerFunc {
	sub, err := fs.Sub(staticFS, "static")
	if err != nil {
		// Build-time error if it ever happens — keep the panic in the
		// constructor so it surfaces immediately on startup, not on
		// first request.
		panic("static embed sub: " + err.Error())
	}
	fileServer := http.FileServer(http.FS(sub))
	return func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/javascript; charset=utf-8")
		w.Header().Set("Cache-Control", "public, max-age=300")
		// Strip the /static/ prefix so the file server resolves against
		// the embedded "static" directory (which we sub'd above).
		r2 := r.Clone(r.Context())
		r2.URL.Path = r.URL.Path[len("/static"):]
		fileServer.ServeHTTP(w, r2)
	}
}
