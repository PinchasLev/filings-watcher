// Server-rendered insider-activity feed at GET /insiders. Lists recent
// "cluster buys" — companies where two or more insiders made open-market
// purchases in a trailing window. This is the cross-filing aggregation that a
// raw Form-4 feed lacks, and the one insider pattern that showed even a modest
// forward edge. Surfacing only; not a scored/ranked signal.

package server

import (
	"html/template"
	"net/http"

	"github.com/PinchasLev/filings-watcher/service/internal/store"
)

const (
	notableWindowDays = 30
	notableFeedLimit  = 60
	// notableMinValue drops de-minimis clusters (a few hundred dollars of
	// director-plan buys) so the feed leads with material activity.
	notableMinValue = 10000.0
)

var insidersTemplate = template.Must(template.New("layout.html.tmpl").Funcs(templateFuncs).ParseFS(
	templateFS, "templates/layout.html.tmpl", "templates/insiders.html.tmpl",
))

type insidersPageData struct {
	Nav        string
	WindowDays int
	Clusters   []store.InsiderCluster
}

func handleInsiders(s storer) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		clusters, err := s.NotableInsiderActivity(r.Context(), notableWindowDays, notableMinValue, notableFeedLimit)
		if err != nil {
			http.Error(w, "query failed", http.StatusInternalServerError)
			return
		}
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		if err := insidersTemplate.ExecuteTemplate(w, "layout.html.tmpl", insidersPageData{
			Nav:        "insiders",
			WindowDays: notableWindowDays,
			Clusters:   clusters,
		}); err != nil {
			// Headers already written; can't change status.
			_ = err
		}
	}
}
