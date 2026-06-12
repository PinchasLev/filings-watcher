// Static-file handler for /static/live.js. The script powers the live
// tape's auto-prepend behavior on /live; the HTML-fragment endpoint
// it polls lives in handlers_live_events.go.

package server

import (
	"embed"
	"io/fs"
	"net/http"
)

//go:embed static/live.js
var staticFS embed.FS

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
